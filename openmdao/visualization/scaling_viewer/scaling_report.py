
"""Define a function to view driver scaling."""
import os
import json
import functools

import numpy as np

from openmdao.core.constants import _SetupStatus, INF_BOUND
import openmdao.utils.hooks as hooks
from openmdao.utils.webview import webview
from openmdao.utils.general_utils import default_noraise
from openmdao.utils.file_utils import _load_and_exec
from openmdao.utils.reports_system import register_report

_default_scaling_filename = 'driver_scaling_report.html'


def _unscale(val, scaler, adder, default=''):
    if val is None:
        return default
    if scaler is not None:
        val = val * (1.0 / scaler)
    if adder is not None:
        val = val - adder
    return val


def _getdef(val, unset):
    if val is None:
        return unset
    if np.isscalar(val) and (val == INF_BOUND or val == -INF_BOUND):
        return unset
    return val


def _get_val_and_size(val, unset=''):
    # return val (or max abs val) and the size of the value
    val = _getdef(val, unset)
    if np.ndim(val) == 0 or val.size == 1:
        return [val, 1]
    return [np.max(np.abs(val)), val.size]


def _get_flat(val, size, unset=''):
    if val is None:
        return val
    if np.isscalar(val):
        if (val == INF_BOUND or val == -INF_BOUND):
            val = unset
        return np.full(size, val)
    if val.size > 1:
        return val.flatten()
    return np.full(size, val[0])


def _add_child_rows(row, mval, dval, scaler=None, adder=None, ref=None, ref0=None,
                    lower=None, upper=None, equals=None, inds=None):
    if not (np.isscalar(mval) or mval.size == 1):
        rowchild = row.copy()
        children = row['_children'] = []
        rowchild['name'] = ''
        rowchild['size'] = ''
        dval_flat = dval.flatten()
        mval_flat = mval.flatten()
        scaler_flat = _get_flat(scaler, mval.size)
        adder_flat = _get_flat(adder, mval.size)
        ref_flat = _get_flat(ref, mval.size)
        ref0_flat = _get_flat(ref0, mval.size)
        upper_flat = _get_flat(upper, mval.size)
        lower_flat = _get_flat(lower, mval.size)
        equals_flat = _get_flat(equals, mval.size)

        if inds is None:
            inds = list(range(dval.size))
        else:
            inds = np.atleast_1d(inds).flatten()

        for i, idx in enumerate(inds):
            d = rowchild.copy()
            d['index'] = idx
            d['driver_val'] = [dval_flat[i], 1]
            d['model_val'] = [mval_flat[i], 1]
            if scaler_flat is not None:
                d['scaler'] = [scaler_flat[i], 1]
            if adder_flat is not None:
                d['adder'] = [adder_flat[i], 1]
            if ref_flat is not None:
                d['ref'] = [ref_flat[i], 1]
            if ref0_flat is not None:
                d['ref0'] = [ref0_flat[i], 1]
            if upper_flat is not None:
                d['upper'] = [upper_flat[i], 1]
            if lower_flat is not None:
                d['lower'] = [lower_flat[i], 1]
            if equals_flat is not None:
                d['equals'] = [equals_flat[i], 1]
            children.append(d)


def _compute_jac_view_info(totals, data, dv_vals, response_vals, coloring):
    start = end = 0
    data['ofslices'] = slices = {}
    for n in data['oflabels']:
        v = response_vals[n]
        end += v.size
        slices[n] = [start, end]
        start = end

    start = end = 0
    data['wrtslices'] = slices = {}
    for n in data['wrtlabels']:
        v = dv_vals[n]
        end += v.size
        slices[n] = [start, end]
        start = end

    nonempty_submats = set()  # submats with any nonzero values

    var_matrix = np.zeros((len(data['oflabels']), len(data['wrtlabels'])))

    matrix = np.abs(totals)

    if coloring is not None:  # factor in the sparsity
        mask = np.zeros(totals.shape, dtype=bool)
        mask[coloring._nzrows, coloring._nzcols] = 1

    for i, of in enumerate(data['oflabels']):
        ofstart, ofend = data['ofslices'][of]
        for j, wrt in enumerate(data['wrtlabels']):
            wrtstart, wrtend = data['wrtslices'][wrt]
            # use max of abs value here instead of norm to keep coloring consistent between
            # top level jac and subjacs
            var_matrix[i, j] = np.max(matrix[ofstart:ofend, wrtstart:wrtend])
            if var_matrix[i, j] > 0. or (coloring is not None and
                                         np.any(mask[ofstart:ofend, wrtstart:wrtend])):
                nonempty_submats.add((of, wrt))

    matlist = [None] * matrix.size
    idx = 0
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            val = matrix[i, j]
            if coloring is not None and not mask[i, j]:
                val = None
            else:
                if val == 0.:
                    val = 0  # set to int 0
            matlist[idx] = [i, j, val]
            idx += 1

    data['mat_list'] = matlist

    varmatlist = [None] * var_matrix.size

    # setup up sparsity of var matrix
    idx = 0
    for i, of in enumerate(data['oflabels']):
        for j, wrt in enumerate(data['wrtlabels']):
            if coloring is not None and (of, wrt) not in nonempty_submats:
                val = None
            else:
                val = var_matrix[i, j]
            varmatlist[idx] = [of, wrt, val]
            idx += 1

    data['var_mat_list'] = varmatlist


def view_driver_scaling(driver, outfile=_default_scaling_filename, show_browser=True,
                        title=None, jac=True):
    """
    Generate a self-contained html file containing a table of scaling data.

    Optionally pops up a web browser to view the file.

    Parameters
    ----------
    driver : Driver
        The driver used for the scaling report.
    outfile : str, optional
        The name of the output html file.  Defaults to 'connections.html'.
    show_browser : bool, optional
        If True, pop up a browser to view the generated html file.
        Defaults to True.
    title : str, optional
        Sets the title of the web page.
    jac : bool
        If True, show jacobian information.

    Returns
    -------
    dict
        Data to used to generate html file.
    """
    dv_table = []
    con_table = []
    obj_table = []

    dv_vals = driver.get_design_var_values(get_remote=True)
    obj_vals = driver.get_objective_values(driver_scaling=True)
    con_vals = driver.get_constraint_values(driver_scaling=True)

    model = driver._problem().model

    mod_meta = model._var_allprocs_abs2meta['output'].copy()  # shallow copy
    mod_meta.update(model._var_allprocs_discrete['output'])

    discretes = {'dvs': [], 'con': [], 'obj': []}

    if driver._problem()._metadata['setup_status'] < _SetupStatus.POST_FINAL_SETUP:
        raise RuntimeError("Driver scaling report cannot be generated before calling final_setup "
                           "on the Problem.")

    default = ''

    idx = 1  # unique ID for use by Tabulator

    def get_inds(dval, meta):
        inds = meta.get('indices')
        if inds is not None:
            inds = inds.as_array()
            if dval.size == 1:
                return inds[0], inds
        return '', inds

    # set up design vars table data
    for name, meta in driver._designvars.items():
        scaler = meta['total_scaler']
        adder = meta['total_adder']
        ref = meta.get('ref')
        ref0 = meta.get('ref0')
        lower = meta.get('lower')
        upper = meta.get('upper')
        src_name = meta.get('source')

        if src_name in model._discrete_outputs:
            discretes['dvs'].append(name)

        dval = dv_vals[name]
        mval = _unscale(dval, scaler, adder, default)

        index, inds = get_inds(dval, meta)

        dct = {
            'id': idx,
            'name': name,
            'size': meta['size'],
            'driver_val': _get_val_and_size(dval),
            'driver_units': _getdef(meta['units'], default),
            'model_val': _get_val_and_size(mval),
            'model_units': _getdef(mod_meta[src_name].get('units'), default),
            'ref': _get_val_and_size(ref, default),
            'ref0': _get_val_and_size(ref0, default),
            'scaler': _get_val_and_size(scaler, default),
            'adder': _get_val_and_size(adder, default),
            'lower': _get_val_and_size(lower, default),  # scaled
            'upper': _get_val_and_size(upper, default),  # scaled
            'index': index,
        }

        dv_table.append(dct)
        _add_child_rows(dct, mval, dval, scaler=scaler, adder=adder, ref=ref, ref0=ref0,
                        lower=lower, upper=upper, inds=inds)

        idx += 1

    # set up constraints table data
    for name, meta in driver._cons.items():
        scaler = meta['total_scaler']
        adder = meta['total_adder']
        ref = meta.get('ref')
        ref0 = meta.get('ref0')
        lower = meta.get('lower')
        upper = meta.get('upper')
        equals = meta.get('equals')
        alias = meta.get('alias', '')
        src_name = meta.get('source')

        if src_name in model._discrete_outputs:
            discretes['con'].append(name)

        dval = con_vals[name]
        mval = _unscale(dval, scaler, adder, default)

        index, inds = get_inds(dval, meta)

        if alias:
            name = meta['name']

        dct = {
            'id': idx,
            'name': name,
            'alias': alias,
            'size': meta['size'],
            'index': index,
            'driver_val': _get_val_and_size(dval),
            'driver_units': _getdef(meta.get('units'), default),
            'model_val': _get_val_and_size(mval),
            'model_units': _getdef(mod_meta[src_name].get('units'), default),
            'ref': _get_val_and_size(ref, default),
            'ref0': _get_val_and_size(ref0, default),
            'scaler': _get_val_and_size(scaler, default),
            'adder': _get_val_and_size(adder, default),
            'lower': _get_val_and_size(lower, default),  # scaled
            'upper': _get_val_and_size(upper, default),  # scaled
            'equals': _get_val_and_size(equals, default),  # scaled
            'linear': meta.get('linear'),
        }

        con_table.append(dct)
        _add_child_rows(dct, mval, dval, scaler=scaler, adder=adder, ref=ref, ref0=ref0,
                        lower=lower, upper=upper, equals=equals, inds=inds)

        idx += 1

    # set up objectives table data
    for name, meta in driver._objs.items():
        scaler = meta['total_scaler']
        adder = meta['total_adder']
        ref = meta.get('ref')
        ref0 = meta.get('ref0')
        alias = meta.get('alias', '')
        src_name = meta.get('source')

        if src_name in model._discrete_outputs:
            discretes['obj'].append(name)

        dval = obj_vals[name]
        mval = _unscale(dval, scaler, adder, default)

        index, inds = get_inds(dval, meta)

        if alias:
            name = meta['name']

        dct = {
            'id': idx,
            'name': name,
            'alias': alias,
            'size': meta['size'],
            'index': index,
            'driver_val': _get_val_and_size(dval),
            'driver_units': _getdef(meta.get('units'), default),
            'model_val': _get_val_and_size(mval),
            'model_units': _getdef(mod_meta[src_name].get('units'), default),
            'ref': _get_val_and_size(meta.get('ref'), default),
            'ref0': _get_val_and_size(meta.get('ref0'), default),
            'scaler': _get_val_and_size(scaler, default),
            'adder': _get_val_and_size(adder, default),
        }

        obj_table.append(dct)
        _add_child_rows(dct, mval, dval, scaler=scaler, adder=adder, ref=ref, ref0=ref0,
                        inds=inds)

        idx += 1

    data = {
        'title': _getdef(title, ''),
        'dv_table': dv_table,
        'con_table': con_table,
        'obj_table': obj_table,
        'oflabels': [],
        'wrtlabels': [],
        'var_mat_list': [],
        'linear': {
            'oflabels': [],
        }
    }

    if jac and not driver._problem().model._use_derivatives:
        print("\nCan't display jacobian because derivatives are turned off.\n")
        jac = False

    elif jac and (discretes['dvs'] or discretes['con'] or discretes['obj']):
        print("\nCan't display jacobian because the following variables are discrete:")
        if discretes['dvs']:
            print(f"  Design Vars: {discretes['dvs']}")
        if discretes['con']:
            print(f"  Constraints: {discretes['con']}")
        if discretes['obj']:
            print(f"  Objectives: {discretes['obj']}")
        jac = False

    if jac:
        # save old totals
        coloring = driver._get_coloring()

        nldvs = driver._get_nl_dvs()
        ldvs = driver._get_lin_dvs()
        lin_dv_vals = {n: dv_vals[n] for n in ldvs}

        # assemble data for jacobian visualization
        if driver._total_jac is None:
            data['oflabels'] = driver._get_ordered_nl_responses()
            data['wrtlabels'] = list(n for n in dv_vals if n in nldvs)

            # this call updates driver._total_jac
            driver._compute_totals(of=data['oflabels'], wrt=data['wrtlabels'],
                                   return_format=driver._total_jac_format)
            totals = driver._total_jac.J  # .J is always an array even if return format != 'array'
            driver._total_jac = None
        else:
            totals = driver._total_jac.J  # .J is always an array even if return format != 'array'
            data['oflabels'] = list(driver._total_jac.output_meta['fwd'])
            data['wrtlabels'] = list(driver._total_jac.input_meta['fwd'])

        data['linear'] = lindata = {}
        lindata['oflabels'] = [n for n, meta in driver._cons.items() if meta['linear']]
        lindata['wrtlabels'] = [n for n in dv_vals if n in ldvs]

        # check for separation of linear constraints
        if lindata['oflabels']:
            if set(lindata['oflabels']).intersection(data['oflabels']):
                # linear cons are found in data['oflabels'] so they're not separated
                lindata['oflabels'] = []
                lindata['wrtlabels'] = []

        full_response_vals = con_vals.copy()
        full_response_vals.update(obj_vals)
        response_vals = {n: full_response_vals[n] for n in data['oflabels']}

        _compute_jac_view_info(totals, data, dv_vals, response_vals, coloring)

        if lindata['oflabels'] and lin_dv_vals:
            lin_response_vals = {n: full_response_vals[n] for n in lindata['oflabels']}

            if driver._total_jac_linear is None:
                # prevent clobbering of nonlinear totals
                save = driver._total_jac
                driver._total_jac = None

                try:
                    lintotals = driver._compute_totals(of=lindata['oflabels'],
                                                       wrt=lindata['wrtlabels'],
                                                       return_format='array')
                finally:
                    driver._total_jac = save
            else:
                lintotals = driver._total_jac_linear.J

            _compute_jac_view_info(lintotals, lindata, lin_dv_vals, lin_response_vals, None)

    if driver._problem().comm.rank == 0:

        viewer = 'scaling_table.html'

        code_dir = os.path.dirname(os.path.abspath(__file__))
        libs_dir = os.path.join(os.path.dirname(code_dir), 'common', 'libs')
        style_dir = os.path.join(os.path.dirname(code_dir), 'common', 'style')

        with open(os.path.join(code_dir, viewer), "r", encoding='utf-8') as f:
            template = f.read()

        with open(os.path.join(libs_dir, 'tabulator.5.4.4.min.js'), "r", encoding='utf-8') as f:
            tabulator_src = f.read()

        with open(os.path.join(style_dir, 'tabulator.5.4.4.min.css'), "r", encoding='utf-8') as f:
            tabulator_style = f.read()

        with open(os.path.join(libs_dir, 'd3.v6.min.js'), "r", encoding='utf-8') as f:
            d3_src = f.read()

        jsontxt = json.dumps(data, default=default_noraise)

        with open(outfile, 'w', encoding='utf-8') as f:
            s = template.replace("<tabulator_src>", tabulator_src)
            s = s.replace("<tabulator_style>", tabulator_style)
            s = s.replace("<d3_src>", d3_src)
            s = s.replace("<scaling_data>", jsontxt)
            f.write(s)

        if show_browser:
            webview(outfile)

    return data


def _scaling_setup_parser(parser):
    """
    Set up the openmdao subparser for the 'openmdao driver_scaling' command.

    Parameters
    ----------
    parser : argparse subparser
        The parser we're adding options to.
    """
    parser.add_argument('file', nargs=1, help='Python file containing the model.')
    parser.add_argument('-o', default=_default_scaling_filename, action='store', dest='outfile',
                        help='html output file.')
    parser.add_argument('-t', '--title', action='store', dest='title',
                        help='title of web page.')
    parser.add_argument('--no_browser', action='store_true', dest='no_browser',
                        help="don't display in a browser.")
    parser.add_argument('-p', '--problem', action='store', dest='problem', help='Problem name')
    parser.add_argument('--no-jac', action='store_true', dest='nojac',
                        help="Don't show jacobian info")


_scaling_report_done = set()


def _exitfunc(probname):
    global _scaling_report_done
    from openmdao.core.problem import _problem_names
    if probname is None:
        probnames = _problem_names
    else:
        probnames = [probname]
    missing = [p for p in probnames if p not in _scaling_report_done]
    if missing:
        print(f"\n\nDriver scaling report(s) not generated for Problem(s) {sorted(missing)}\n")


def _check_nl_totals(driver, **kwargs):
    # prevent hook from triggering until we have computed the total jacobian for the nonlinear
    # constraints and objectives
    return driver._total_jac is not None


def _scaling_cmd(options, user_args):
    """
    Return the post_setup hook function for 'openmdao driver_scaling'.

    Parameters
    ----------
    options : argparse Namespace
        Command line options.
    user_args : list of str
        Args to be passed to the user script.
    """
    # disable the reports system, we only want the scaling report and then we exit
    os.environ['OPENMDAO_REPORTS'] = '0'

    def _do_scaling_report(driver, infile='', outfile=_default_scaling_filename, show_browser=True,
                           title=None, jac=True):
        global _scaling_report_done
        _scaling_report_done.add(driver._problem()._name)
        if title is None:
            title = f"Driver scaling for {infile}"
        driver.scaling_report(outfile=outfile, show_browser=show_browser, title=title, jac=jac)

    hooks._register_hook('_compute_totals', class_name='Driver', inst_id=options.problem,
                         post=_do_scaling_report, ncalls=1, predicate=_check_nl_totals,
                         infile=options.file[0], outfile=options.outfile,
                         show_browser=not options.no_browser, title=options.title,
                         jac=not options.nojac)

    # register an atexit function to check if scaling report was triggered during the script
    import atexit
    atexit.register(functools.partial(_exitfunc, options.problem))

    _load_and_exec(options.file[0], user_args)


# scaling report definition
def _run_scaling_report(driver, report_filename=_default_scaling_filename):

    prob = driver._problem()
    scaling_filepath = prob.get_reports_dir() / report_filename

    if not driver.supports['optimization']:
        return

    try:
        prob.driver.scaling_report(outfile=scaling_filepath, show_browser=False)

    # Need to handle the coloring and scaling reports which can fail in this way
    # because total Jacobian can't be computed
    except RuntimeError as err:
        if str(err) != "Can't compute total derivatives unless " \
                       "both 'of' or 'wrt' variables have been specified.":
            raise err


def _scaling_report_register():
    register_report('scaling', _run_scaling_report, 'Driver scaling report', 'Driver',
                    '_compute_totals', 'post', predicate=_check_nl_totals)
