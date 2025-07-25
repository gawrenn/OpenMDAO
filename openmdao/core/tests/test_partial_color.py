import sys
import unittest
import itertools
from fnmatch import fnmatchcase

import numpy as np
from scipy.sparse import coo_matrix


try:
    import jax
    import jax.numpy as jnp
except ImportError:
    jax = None

from openmdao.api import Problem, Group, IndepVarComp, ImplicitComponent, ExplicitComponent, \
    NonlinearBlockGS, ScipyOptimizeDriver, NewtonSolver, DirectSolver, ImplicitFuncComp
from openmdao.core.problem import _clear_problem_names
import openmdao.func_api as omf
from openmdao.utils.assert_utils import assert_near_equal, assert_warning
from openmdao.utils.array_utils import evenly_distrib_idxs
from openmdao.utils.testing_utils import require_pyoptsparse, use_tempdirs, parameterized_name
from openmdao.utils.mpi import MPI
from openmdao.utils.coloring import compute_total_coloring


try:
    from openmdao.parallel_api import PETScVector
except ImportError:
    PETScVector = None

try:
    from parameterized import parameterized
except ImportError:
    from openmdao.utils.assert_utils import SkipParameterized as parameterized

from openmdao.utils.general_utils import set_pyoptsparse_opt


# check that pyoptsparse is installed. if it is, try to use SLSQP.
OPT, OPTIMIZER = set_pyoptsparse_opt('SLSQP')
if OPTIMIZER:
    from openmdao.drivers.pyoptsparse_driver import pyOptSparseDriver
else:
    pyOptSparseDriver = None


def setup_vars(comp, ofs, wrts, sparse_partials=False, bad_sparsity=False):
    matrix = comp.sparsity
    isplit = comp.isplit
    osplit = comp.osplit

    slices = {}
    isizes, _ = evenly_distrib_idxs(isplit, matrix.shape[1])
    start = end = 0
    for i, sz in enumerate(isizes):
        end += sz
        wrt = f"x{i}"
        comp.add_input(wrt, np.zeros(sz))
        slices[wrt] = slice(start, end)
        start = end

    osizes, _ = evenly_distrib_idxs(osplit, matrix.shape[0])
    start = end = 0
    for i, sz in enumerate(osizes):
        end += sz
        of = f"y{i}"
        comp.add_output(of, np.zeros(sz))
        slices[of] = slice(start, end)
        start = end

    if sparse_partials:
        nbad = 0
        for i in range(len(osizes)):
            of = f"y{i}"
            for j in range(len(isizes)):
                wrt = f"x{j}"
                subjac = comp.sparsity[slices[of], slices[wrt]]
                if np.any(subjac):
                    rows, cols = np.nonzero(subjac)
                    if bad_sparsity and rows.size > 1 and nbad < 5:
                        rows[rows.size // 2] = -1  # remove one row/col pair
                        mask = rows != -1
                        rows = rows[mask]
                        cols = cols[mask]
                        nbad += 1
                    comp.declare_partials(of=of, wrt=wrt, rows=rows, cols=cols, method=comp.method)
                else:
                    comp.declare_partials(of=of, wrt=wrt, method=comp.method, dependent=False)
    else:
        comp.declare_partials(of=ofs, wrt=wrts, method=comp.method)


def setup_sparsity(mask):
    return np.random.random(mask.shape) * mask


def setup_indeps(isplit, ninputs, indeps_name, comp_name):
    isizes, _ = evenly_distrib_idxs(isplit, ninputs)
    indeps = IndepVarComp()
    conns = []
    for i, sz in enumerate(isizes):
        indep_var = 'x%d' % i
        indeps.add_output(indep_var, np.random.random(sz))
        conns.append((indeps_name + '.' + indep_var, comp_name + '.' + indep_var))

    return indeps, conns


class CounterGroup(Group):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._nruns = 0

    def _solve_nonlinear(self, *args, **kwargs):
        super()._solve_nonlinear(*args, **kwargs)
        self._nruns += 1


class SparseCompImplicit(ImplicitComponent):

    def __init__(self, sparsity, method='fd', isplit=1, osplit=1, **kwargs):
        super().__init__(**kwargs)
        self.sparsity = sparsity
        self.isplit = isplit
        self.osplit = osplit
        self.method = method
        self._nruns = 0

    def setup(self):
        setup_vars(self, ofs='*', wrts='*')

    # this is defined for easier testing of coloring of approx partials
    def apply_nonlinear(self, inputs, outputs, residuals):
        prod = self.sparsity.dot(inputs.asarray()) - outputs.asarray()
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            residuals[outname] = prod[start:end]
            start = end
        self._nruns += 1

    # this is defined so we can more easily test coloring of approx totals in a Group above this comp
    def solve_nonlinear(self, inputs, outputs):
        prod = self.sparsity.dot(inputs.asarray())
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            outputs[outname] = prod[start:end]
            start = end
        self._nruns += 1


# needed to wrap the sparsity array in this class in order to make it hashable so jax would be happy
class SpObj(object):
    def __init__(self, sparsity):
        self.mat = sparsity
        self._nruns = 0


class SparseFuncCompImplicit(ImplicitFuncComp):

    def __init__(self, sparsity, method='cs', isplit=1, osplit=1, **kwargs):
        self.sparsity = sparsity
        self.isplit = isplit
        self.osplit = osplit
        self.method = method
        self._nruns = 0

        isizes, _ = evenly_distrib_idxs(self.isplit, self.sparsity.shape[1])
        wrts = [(f"x{i}", sz) for i, sz in enumerate(isizes)]

        osizes, _ = evenly_distrib_idxs(self.osplit, self.sparsity.shape[0])
        ofs = [(f"y{i}", sz) for i, sz in enumerate(osizes)]

        # define apply_nonlinear function
        iparams = ','.join([n for n,_ in wrts])
        oparams = ','.join([n for n,_ in ofs])
        params = ','.join((iparams, oparams))
        flines = ['def func(' + params + ',sparsity):']
        if len(wrts) > 1:
            flines.append(f'    iarr = np.concatenate(({iparams}))')
        else:
            flines.append(f'    iarr = {iparams[0]}0')
        if len(ofs) > 1:
            flines.append(f'    oarr = np.concatenate(({oparams}))')
        else:
            flines.append(f'    oarr = {oparams[0]}0')
        flines.append('    prod = sparsity.mat.dot(iarr) - oarr')
        flines.append('    sparsity._nruns += 1')
        start = end = 0
        for name, sz in ofs:
            end += sz
            flines.append(f"    r{name} = prod[{start}:{end}]")
            start = end
        flines.append('    return ' + ','.join([f"r{n}" for n,_ in ofs]))
        fbody = '\n'.join(flines)

        if sys.version_info >= (3,13):
            # https://docs.python.org/3/whatsnew/3.13.html#pep667-porting-notes-py
            # Code execution functions that implicitly target locals() (such as exec and eval)
            # must be passed an explicit namespace to access their results in an optimized scope.
            tmp_locals = dict()
            exec(fbody, locals=tmp_locals)  # nosec trusted input
            f = omf.wrap(tmp_locals['func'])
        else:
            exec(fbody)  # nosec trusted input
            f = omf.wrap(locals()['func'])

        for name, sz in wrts:
            f.add_input(name, shape=sz)
        for name, sz in ofs:
            f.add_output(name, shape=sz, resid=f"r{name}")
        f.declare_option('sparsity', default=SpObj(sparsity))
        f.declare_partials(of='*', wrt='*', method=self.method)

        super().__init__(f, **kwargs)

    # this is defined for easier testing of coloring of approx partials
    def apply_nonlinear(self, inputs, outputs, residuals):
        super().apply_nonlinear(inputs, outputs, residuals)
        self._nruns += 1

    # this is defined so we can more easily test coloring of approx totals in a Group above this comp
    def solve_nonlinear(self, inputs, outputs):
        prod = self.sparsity.dot(inputs.asarray())
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            outputs[outname] = prod[start:end]
            start = end
        self._nruns += 1


class SparseCompExplicit(ExplicitComponent):

    def __init__(self, sparsity, method='fd', isplit=1, osplit=1, sparse_partials=False, bad_sparsity=False, **kwargs):
        super().__init__(**kwargs)
        self.sparsity = sparsity
        self.isplit = isplit
        self.osplit = osplit
        self.method = method
        self.sparse_partials = sparse_partials
        self.bad_sparsity = bad_sparsity
        self._nruns = 0

    def setup(self):
        setup_vars(self, ofs='*', wrts='*', sparse_partials=self.sparse_partials, bad_sparsity=self.bad_sparsity)

    def compute(self, inputs, outputs):
        prod = self.sparsity.dot(inputs.asarray())
        start = end = 0
        for i in range(self.osplit):
            outname = 'y%d' % i
            end += outputs[outname].size
            outputs[outname] = prod[start:end]
            start = end
        self._nruns += 1


# relative tolerances for jacobian checks
_TOLS = {
    'fd': 1e-6,
    'cs': 1e-12,
    'jax': 1e-7
}

def _check_partial_matrix(system, jac, expected, method):
    blocks = []
    for abs_of, ofmeta in system._var_allprocs_abs2meta['output'].items():
        cblocks = []
        for abs_wrt, wrtmeta in system._var_allprocs_abs2meta['input'].items():
            key = (abs_of, abs_wrt)
            if key in jac:
                meta = jac[key]
                if meta['rows'] is not None:
                    cblocks.append(coo_matrix((meta['val'], (meta['rows'], meta['cols'])), shape=meta['shape']).toarray())
                elif meta['dependent']:
                    cblocks.append(meta['val'])
                else:
                    cblocks.append(np.zeros(meta['shape']))
            else: # sparsity was all zeros so we declared this subjac as not dependent
                relof = abs_of.rsplit('.', 1)[-1]
                relwrt = abs_wrt.rsplit('.', 1)[-1]
                for decl_key, meta in system._declared_partials_patterns.items():
                    if not meta['dependent']:
                        ofpats, wrtpats = decl_key
                        done = False
                        for p in ofpats:
                            if fnmatchcase(relof, p):
                                for wp in wrtpats:
                                    if fnmatchcase(relwrt, wp):
                                        cblocks.append(np.zeros((ofmeta['size'], wrtmeta['size'])))
                                        done = True
                                        break
                                if done:
                                    break
                if (relof, relwrt) in system._declared_partials_patterns and not system._declared_partials_patterns[(relof, relwrt)].get('dependent'):
                    cblocks.append(np.zeros((ofmeta['size'], wrtmeta['size'])))
        if cblocks:
            blocks.append(np.hstack(cblocks))
    fullJ = np.vstack(blocks)
    assert_near_equal(fullJ, expected, tolerance=_TOLS[method], tol_type='rel')


def _check_total_matrix(system, jac, expected, method):
    ofs = {}
    for key, subjac in jac.items():
        of, wrt = key
        if of not in ofs:
            ofs[of] = [subjac]
        else:
            ofs[of].append(subjac)

    for of, sjacs in ofs.items():
        ofs[of] = np.hstack(sjacs)

    fullJ = np.vstack(list(ofs.values()))
    assert_near_equal(fullJ, expected, tolerance=_TOLS[method], tol_type='rel')


def _check_semitotal_matrix(system, jac, expected, method):
    blocks = []
    for of in system._var_allprocs_abs2meta['output']:
        cblocks = []
        for wrt in itertools.chain(system._var_allprocs_abs2meta['output'], system._var_allprocs_abs2meta['input']):
            key = (of, wrt)
            if key in jac:
                rows = jac[key]['rows']
                if rows is not None:
                    cols = jac[key]['cols']
                    val = coo_matrix((jac[key]['val'], (rows, cols)), shape=jac[key]['shape']).toarray()
                else:
                    val = jac[key]['val']
                cblocks.append(val)
        if cblocks:
            blocks.append(np.hstack(cblocks))
    fullJ = np.vstack(blocks)
    assert_near_equal(fullJ, expected, tolerance=_TOLS[method], tol_type='rel')


_BIGMASK = np.array(
    [[1, 0, 0, 1, 1, 1, 0, 1, 0, 0, 0, 1, 1, 0, 0, 0, 0, 1, 1],
     [0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1],
     [0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 1],
     [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0],
     [0, 1, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0],
     [0, 1, 0, 1, 1, 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1],
     [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1],
     [0, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 1, 0],
     [0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0, 0],
     [1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 0, 0],
     [0, 1, 1, 0, 1, 1, 1, 0, 1, 0, 0, 1, 0, 0, 0, 0, 1, 1, 1]]
)

@use_tempdirs
class TestColoringExplicit(unittest.TestCase):
    def setUp(self):
        np.random.seed(11)

    def test_partials_explicit(self):

        for method, isplit, osplit, sparse_partials in itertools.product(['fd', 'cs'],
                                                                         [1,2,7,19],
                                                                         [1,2,5,11],
                                                                         [True, False]):

            with self.subTest(msg=f'{method=} {isplit=} {osplit=} {sparse_partials=}'):
                prob = Problem(name=f'test_partials_explicit_{method}_'
                               f'{isplit}_{osplit}_{sparse_partials}')
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')
                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit,
                                                                    sparse_partials=sparse_partials))
                comp.declare_coloring('x*', method=method)

                for conn in conns:
                    model.connect(*conn)

                prob.setup(mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                comp.run_linearize()
                prob.run_model()
                start_nruns = comp._nruns
                comp.run_linearize()
                self.assertEqual(comp._nruns - start_nruns, 10)
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)

    def test_partials_explicit_static(self):
        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,7,19],
                                                        [1,2,5,11]):

            with self.subTest(msg=f'{method=} {isplit=} {osplit=}'):
                probname = f'test_partials_explicit_static_{method}_{isplit}_{osplit}'
                prob = Problem(name=probname)
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')
                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit))
                for conn in conns:
                    model.connect(*conn)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                coloring = comp._compute_coloring(wrt_patterns='x*', method=method)[0]
                comp._save_coloring(coloring)

                # This is a hack to get around the restriction of duplicate
                # problem names in the same execution of the same script.
                _clear_problem_names()

                # now make a second problem to use the coloring
                prob = Problem(name=probname)
                model = prob.model
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit))
                for conn in conns:
                    model.connect(*conn)

                comp.declare_coloring(wrt='x*', method=method)
                comp.use_fixed_coloring()
                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                comp._linearize()
                self.assertEqual(comp._nruns - start_nruns, 10)
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)

    def test_partials_explicit_reuse(self):
        for num_insts in [1, 2, 5]:
            with self.subTest(msg=f'{num_insts=}'):
                method = 'cs'
                osplit = 5
                isplit = 7
                prob = Problem(name=f'test_partials_explicit_reuse_{num_insts}')
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')
                model.add_subsystem('indeps', indeps)

                comps = []
                for i in range(num_insts):
                    cname = 'comp%d' % i
                    comp = model.add_subsystem(cname, SparseCompExplicit(sparsity, method,
                                                                        isplit=isplit, osplit=osplit))
                    comp.declare_coloring('x*', method=method, per_instance=False)
                    comps.append(comp)

                    _, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', cname)

                    for conn in conns:
                        model.connect(*conn)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                prob.model.run_linearize()
                prob.run_model()
                start_nruns = [c._nruns for c in comps]
                for i, comp in enumerate(comps):
                    comp.run_linearize()
                    self.assertEqual(comp._nruns - start_nruns[i], 10)
                    jac = comp._jacobian._subjacs_info
                    _check_partial_matrix(comp, jac, sparsity, method)

                orig = comps[0]._coloring_info.coloring
                for comp in comps:
                    self.assertTrue(orig is comp._coloring_info.coloring,
                                    "Instance '{}' is using a different coloring".format(comp.pathname))


@use_tempdirs
class TestColoringImplicit(unittest.TestCase):
    def setUp(self):
        np.random.seed(11)

    def test_partials_implicit(self):
        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,7,19],
                                                        [1,2,5,11]):
            with self.subTest(msg=f'{method=}_{isplit=}_{osplit=}'):
                prob = Problem(name=f'test_partials_implicit_{method}_{isplit}_{osplit}')
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')
                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit))
                comp.declare_coloring('x*', method=method)

                for conn in conns:
                    model.connect(*conn)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                comp.run_linearize()
                prob.run_model()
                start_nruns = comp._nruns
                comp.run_linearize()
                self.assertEqual(comp._nruns - start_nruns, 10 + sparsity.shape[0])
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)

    def test_simple_partials_implicit_static(self):
        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,7,19],
                                                        [1,2,5,11]):
            with self.subTest(msg=f'{method=}_{isplit=}_{osplit=}'):
                prob = Problem(name=f'test_simple_partials_implicit_static_{method}_{isplit}_{osplit}')
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit))
                for conn in conns:
                    model.connect(*conn)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                coloring = comp._compute_coloring(wrt_patterns='x*', method=method)[0]
                comp._save_coloring(coloring)

                # now create a new problem and set the static coloring
                _clear_problem_names()  # We don't care about a duplicate problem name in this test.
                prob = Problem(name=f'test_simple_partials_implicit_static_{method}_{isplit}_{osplit}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method,
                                                                    isplit=isplit, osplit=osplit))
                for conn in conns:
                    model.connect(*conn)

                comp.declare_coloring(wrt='x*', method=method)
                comp.use_fixed_coloring()
                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                comp._linearize()
                # add 5 to number of runs to cover the 5 uncolored output columns
                self.assertEqual(comp._nruns - start_nruns, sparsity.shape[0] + 10)
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)


@use_tempdirs
class TestColoringImplicitFuncComp(unittest.TestCase):
    def setUp(self):
        np.random.seed(11)

    def test_partials_implicit_funccomp(self):
        for (method, direction), isplit, osplit in itertools.product([('fd', 'fwd'), ('cs', 'fwd'), ('jax', 'fwd'), ('jax', 'rev')] if jax else [('fd', 'fwd'), ('cs', 'fwd')],
        [1,2,7,19],
        [1,2,5,11]):
            with self.subTest(msg=f'{method=}_{direction=}_{isplit=}_{osplit=}'):
                prob = Problem(name=f'test_partials_implicit_funccomp_{method}_{direction}_{isplit}_{osplit}')
                model = prob.model

                if direction == 'rev':
                    mat = np.vstack((_BIGMASK, _BIGMASK)).T
                    ninputs = mat.shape[1]
                    sparsity = setup_sparsity(mat)
                else:
                    ninputs = _BIGMASK.shape[1]
                    sparsity = setup_sparsity(_BIGMASK)

                indeps, conns = setup_indeps(isplit, ninputs, 'indeps', 'comp')
                model.add_subsystem('indeps', indeps)
                if method == 'jax':
                    sparsity = jnp.array(sparsity)
                comp = model.add_subsystem('comp', SparseFuncCompImplicit(sparsity, method,
                                                                        isplit=isplit, osplit=osplit))
                comp.declare_coloring('*', method=method)

                for conn in conns:
                    model.connect(*conn)

                prob.setup(check=False, mode=direction)
                prob.set_solver_print(level=0)
                prob.run_model()

                comp.run_linearize()
                prob.run_model()
                comp.run_linearize()
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)


@use_tempdirs
class TestColoringSemitotals(unittest.TestCase):

    def setUp(self):
        np.random.seed(11)

    def test_simple_semitotals(self):
        self.skipTest('Semi-total coloring currently not supported.')
        for method, isplit, osplit, sparse_partials in itertools.product(['fd', 'cs'],[1,2,19],[1,2,11],[True, False]):
            with self.subTest(f'{method=} {isplit=} {osplit=} {sparse_partials=}'):
                probname = f'test_simple_semitotals_{method}_{isplit}_{osplit}_{sparse_partials}'
                prob = Problem(name=probname)
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'sub.comp')

                model.add_subsystem('indeps', indeps)
                sub = model.add_subsystem('sub', CounterGroup())
                sub.declare_coloring('*', method=method)
                sub.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=osplit,
                                                             sparse_partials=sparse_partials))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.sub.comp.add_constraint('y%d' % i)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                prob.driver._compute_totals()  # this is when the dynamic coloring update happens

                start_nruns = sub._nruns
                prob.driver._compute_totals()
                _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity, method)
                self.assertEqual(sub._nruns - start_nruns, 10)

    def test_simple_semitotals_static(self):
        self.skipTest('Semi-total coloring currently not supported.')

        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,19],
                                                        [1,2,11]):
            with self.subTest(msg=f'{method=} {isplit=} {osplit=}'):
                probname = f'test_simple_partials_implicit_static_{method}_{isplit}_{osplit}'

                prob = Problem(name=probname)
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'sub.comp')

                model.add_subsystem('indeps', indeps)
                sub = model.add_subsystem('sub', Group())
                comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=osplit))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.sub.comp.add_constraint('y%d' % i)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                coloring = sub._compute_coloring(wrt_patterns='comp.x*', method=method)[0]
                sub._save_coloring(coloring)

                # now create a second problem and use the static coloring
                prob = Problem(name=probname)
                model = prob.model

                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'sub.comp')

                model.add_subsystem('indeps', indeps)
                sub = model.add_subsystem('sub', Group())
                comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=osplit))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.sub.comp.add_constraint('y%d' % i)

                sub.declare_coloring(wrt='comp.x*', method=method)
                sub.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                prob.driver._compute_totals()

                nruns = comp._nruns - start_nruns
                self.assertEqual(nruns, 10)
                _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity, method)

    def test_semitotals_unsupported(self):
        prob = Problem(name='test_semitotals_unsupported')
        model = prob.model

        sparsity = setup_sparsity(_BIGMASK)
        indeps, conns = setup_indeps(1, _BIGMASK.shape[1], 'indeps', 'sub.comp')

        model.add_subsystem('indeps', indeps)
        sub = model.add_subsystem('sub', CounterGroup())
        sub.declare_coloring('*', method='fd')
        sub.add_subsystem('comp', SparseCompExplicit(sparsity, 'fd', isplit=1, osplit=1,
                                                     sparse_partials=False))
        for conn in conns:
            model.connect(*conn)

        with self.assertRaises(RuntimeError) as err:
            prob.setup(check=False)

        expected_msg = "'sub' <class CounterGroup>: semi-total coloring is currently not supported."
        self.assertEqual(str(err.exception), expected_msg)


@use_tempdirs
class TestColoring(unittest.TestCase):

    def setUp(self):
        np.random.seed(11)

    def test_partials_explicit_shape_bug(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_partials_explicit_shape_bug_{method}')
                model = prob.model

                # create sparsity with last row and col all zeros.
                # bug caused an exception when we created a COO matrix without supplying shape
                mask = np.array(
                        [[1, 0, 0, 1, 1, 1, 0],
                        [0, 1, 0, 1, 0, 1, 0],
                        [0, 1, 0, 1, 1, 1, 0],
                        [1, 0, 0, 0, 0, 1, 0],
                        [0, 0, 0, 0, 0, 0, 0]]
                    )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=2))
                comp.declare_coloring('x*', method=method)

                for conn in conns:
                    model.connect(*conn)

                prob.setup(check=False, mode='fwd')

                prob.set_val('indeps.x0', [1.03, 1.04, 1.05, 1.06])
                prob.set_val('indeps.x1', [1.06, 1.07, 1.08])

                prob.set_solver_print(level=0)
                prob.run_model()

                comp._linearize()
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)

    def test_partials_min_improvement(self):
        prob = Problem(name='test_partials_min_improvement')
        model = prob.model

        mask = np.array(
                [[1, 0, 1, 0, 1, 1],
                 [1, 1, 1, 0, 0, 1],
                 [0, 1, 0, 1, 1, 0],
                 [1, 0, 1, 0, 0, 1]]
            )

        isplit = 2
        sparsity = setup_sparsity(mask)
        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, 'cs',
                                                              isplit=isplit, osplit=2))
        comp.declare_coloring('x*', method='cs', min_improve_pct=20)

        for conn in conns:
            model.connect(*conn)

        prob.setup(check=False, mode='fwd')

        prob.set_solver_print(level=0)
        prob.run_model()
        with assert_warning(UserWarning, "'comp' <class SparseCompExplicit>: Coloring was deactivated.  Improvement of 16.7% was less than min allowed (20.0%)."):
            prob.model._linearize(None)

        start_nruns = comp._nruns
        comp._linearize()
        # verify we're doing a solve for each column
        self.assertEqual(6, comp._nruns - start_nruns)

        self.assertEqual(comp._coloring_info.coloring, None)
        self.assertEqual(comp._coloring_info['static'], None)

        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity, 'cs')

    def test_partials_min_improvement_reuse(self):
        prob = Problem(name='test_partials_min_improvement_reuse')
        model = prob.model

        mask = np.array(
                [[1, 0, 1, 0, 1, 1],
                 [1, 1, 1, 0, 0, 1],
                 [0, 1, 0, 1, 1, 0],
                 [1, 0, 1, 0, 0, 1]]
            )

        isplit = 2
        sparsity = setup_sparsity(mask)
        indeps, _ = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)

        comps = []
        for i in range(3):
            cname = 'comp%d' % i
            comp = model.add_subsystem(cname, SparseCompExplicit(sparsity, 'cs',
                                                                isplit=isplit, osplit=2))
            comp.declare_coloring('x*', method='cs', min_improve_pct=20, per_instance=False)
            comps.append(comp)
            _, conns = setup_indeps(isplit, mask.shape[1], 'indeps', cname)

            for conn in conns:
                model.connect(*conn)

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        for i, comp in enumerate(comps):
            if i == 0:
                with assert_warning(UserWarning, "'comp0' <class SparseCompExplicit>: Coloring was deactivated.  Improvement of 16.7% was less than min allowed (20.0%)."):
                    comp._linearize()

            start_nruns = comp._nruns
            comp._linearize()
            self.assertEqual(6, comp._nruns - start_nruns)
            self.assertEqual(comp._coloring_info.coloring, None)
            self.assertEqual(comp._coloring_info['static'], None)

            jac = comp._jacobian._subjacs_info
            _check_partial_matrix(comp, jac, sparsity, 'cs')

    @unittest.skipUnless(OPTIMIZER, 'requires pyoptsparse SLSQP.')
    def test_simple_totals(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_simple_totals_{method}')
                model = prob.model = CounterGroup()
                prob.driver = pyOptSparseDriver(optimizer='SLSQP')
                prob.driver.declare_coloring()

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [1, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                model.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')
                model.declare_coloring('*', method=method, step=1e-6 if method=='fd' else None)

                model.comp.add_objective('y0', index=0)  # pyoptsparse SLSQP requires a scalar objective, so pick index 0
                model.comp.add_constraint('y1', lower=[1., 2.])
                model.add_design_var('indeps.x0', lower=np.ones(3), upper=np.ones(3)+.1)
                model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)
                model.approx_totals(method=method)
                prob.setup(check=False, mode='fwd')

                prob.set_val('indeps.x0', [1.03, 1.04, 1.05])
                prob.set_val('indeps.x1', [1.06, 1.07])

                prob.set_solver_print(level=0)
                prob.run_driver()  # need this to trigger the dynamic coloring

                prob.driver._total_jac = None

                start_nruns = model._nruns
                derivs = prob.compute_totals()
                _check_total_matrix(model, derivs, sparsity[[0,3,4],:], method)
                nruns = model._nruns - start_nruns
                self.assertEqual(nruns, 3)

    @require_pyoptsparse(optimizer='SLSQP')
    def test_simple_totals_min_improvement(self):
        for optim in [pyOptSparseDriver, ScipyOptimizeDriver]:
            with self.subTest(msg=f'{optim=}'):
                str_optim = 'pyOptSparseDriver' if optim is \
                    pyOptSparseDriver else 'ScipyOptimizeDrier'
                prob = Problem(name=f'test_simple_totals_min_improvement_{str_optim}')
                model = prob.model = CounterGroup()
                prob.driver = optim(optimizer='SLSQP')

                prob.driver.declare_coloring()

                mask = np.array(
                    [[1, 0, 1, 1, 1],
                    [1, 1, 0, 1, 1],
                    [0, 1, 1, 1, 1],
                    [1, 0, 1, 0, 0],
                    [0, 1, 1, 0, 1]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                model.add_subsystem('comp', SparseCompExplicit(sparsity, 'cs', isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_objective('y0', index=0)  # pyoptsparse SLSQP requires a scalar objective, so pick index 0
                model.comp.add_constraint('y1', lower=[1., 2.])
                model.add_design_var('indeps.x0', lower=np.ones(3), upper=np.ones(3)+.1)
                model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)
                model.approx_totals(method='cs')
                model.declare_coloring(min_improve_pct=25., method='cs')
                prob.setup(check=False, mode='fwd')

                prob.set_val('indeps.x0', [1.03, 1.04, 1.05])
                prob.set_val('indeps.x1', [1.06, 1.07])

                prob.set_solver_print(level=0)

                with assert_warning(UserWarning, "<model> <class CounterGroup>: Coloring was deactivated.  Improvement of 20.0% was less than min allowed (25.0%)."):
                    prob.run_driver()  # need this to trigger the dynamic coloring

                prob.driver._total_jac = None

                start_nruns = model._nruns
                derivs = prob.compute_totals()
                nruns = model._nruns - start_nruns
                _check_total_matrix(model, derivs, sparsity[[0,3,4],:], 'cs')
                self.assertEqual(nruns, 5)

    @require_pyoptsparse(optimizer='SLSQP')
    def test_totals_over_implicit_comp(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_totals_over_implicit_comp_{method}')
                model = prob.model = CounterGroup()
                prob.driver = pyOptSparseDriver(optimizer='SLSQP')
                prob.driver.declare_coloring()

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [1, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.nonlinear_solver = NonlinearBlockGS()
                model.add_subsystem('indeps', indeps)
                model.add_subsystem('comp', SparseCompImplicit(sparsity, method, isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_objective('y0', index=1)
                model.comp.add_constraint('y1', lower=[1., 2.])
                model.add_design_var('indeps.x0', lower=np.ones(3), upper=np.ones(3)+.1)
                model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)

                model.approx_totals(method=method)

                prob.setup(check=False, mode='fwd')

                prob.set_val('indeps.x0', [1.03, 1.04, 1.05])
                prob.set_val('indeps.x1', [1.06, 1.07])

                prob.set_solver_print(level=0)
                prob.run_driver()  # need this to trigger the dynamic coloring

                prob.driver._total_jac = None

                start_nruns = model._nruns
                derivs = prob.driver._compute_totals()
                self.assertEqual(model._nruns - start_nruns, 3)
                rows = [1,3,4]
                _check_total_matrix(model, derivs, sparsity[rows, :], method)

    @parameterized.expand(itertools.product(['fd', 'cs'],[True, False]), name_func=parameterized_name)
    @unittest.skipUnless(OPTIMIZER, 'requires pyoptsparse SLSQP.')
    def test_totals_of_wrt_indices(self, method, sparse_partials):
        prob = Problem(name=f'test_totals_of_wrt_indices_{method}_{sparse_partials}')
        model = prob.model = CounterGroup()
        prob.driver = pyOptSparseDriver(optimizer='SLSQP')
        prob.driver.declare_coloring()

        mask = np.array(
            [[1, 0, 0, 1, 1],
            [0, 1, 0, 1, 1],
            [0, 1, 0, 1, 1],
            [1, 0, 0, 0, 0],
            [0, 1, 1, 0, 0]]
        )

        isplit=2
        sparsity = setup_sparsity(mask)
        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                        isplit=isplit, osplit=2,
                                                        sparse_partials=sparse_partials))

        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        model.comp.add_objective('y0', index=1)
        model.comp.add_constraint('y1', lower=[1., 2.])
        model.add_design_var('indeps.x0',  indices=[0,2], lower=np.ones(2), upper=np.ones(2)+.1)
        model.add_design_var('indeps.x1', lower=np.ones(2), upper=np.ones(2)+.1)

        model.approx_totals(method=method)

        prob.setup(check=False, mode='fwd')

        prob.set_val('indeps.x0', [1.03, 1.04, 1.05])
        prob.set_val('indeps.x1', [1.06, 1.07])

        prob.set_solver_print(level=0)
        prob.run_driver()  # need this to trigger the dynamic coloring

        prob.driver._total_jac = None

        start_nruns = model._nruns
        derivs = prob.driver._compute_totals()  # colored

        self.assertEqual(model._nruns - start_nruns, 2)
        cols = [0,2,3,4]
        rows = [1,3,4]
        _check_total_matrix(model, derivs, sparsity[rows, :][:, cols], method)

    def test_no_solver_linearize(self):
        # this raised a singularity error before the fix
        class Get_val_imp(ImplicitComponent):
            def initialize(self):
                self.options.declare('size',default=1)
            def setup(self):
                size = self.options['size']
                self.add_output('state',val=5.0*np.ones(size))
                self.add_input('bar',val=1.4*np.ones(size))
                self.add_input('foobar')

                self.nonlinear_solver = NewtonSolver()
                self.linear_solver = DirectSolver()

                self.nonlinear_solver.options['iprint'] = 2
                self.nonlinear_solver.options['maxiter']=1
                self.nonlinear_solver.options['solve_subsystems']=False

                self.declare_partials(of="*", wrt="*", method="fd")

                self.declare_coloring(method="fd", num_full_jacs=2)

            def apply_nonlinear(self,inputs,outputs,residuals):
                foo = outputs['state']
                bar = inputs['bar']

                area_ratio = 1 / foo * np.sqrt(1/(bar+1)* (1/foo**2))

                residuals['state']=inputs['foobar']-area_ratio

        size = 3

        ivc = IndepVarComp()
        ivc.add_output('bar',val=1.4*np.ones(size))
        ivc.add_output('foobar',val=40)


        p = Problem()
        p.model.add_subsystem('ivc',ivc,promotes=['*'])
        p.model.add_subsystem('comp_check', Get_val_imp(size=size))
        p.model.connect('bar','comp_check.bar')
        p.setup()
        p.run_model()


@use_tempdirs
class TestStaticColoring(unittest.TestCase):

    def setUp(self):
        np.random.seed(11)

    def test_partials_explicit_shape_bug(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_partials_explicit_shape_bug_static_{method}')
                model = prob.model

                # create sparsity with last row and col all zeros.
                # bug happened when we created a COO matrix without supplying shape
                mask = np.array(
                        [[1, 0, 0, 1, 1, 1, 0],
                        [0, 1, 0, 1, 0, 1, 0],
                        [0, 1, 0, 1, 1, 1, 0],
                        [1, 0, 0, 0, 0, 1, 0],
                        [0, 0, 0, 0, 0, 0, 0]]
                    )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                coloring = comp._compute_coloring(wrt_patterns='x*', method=method)[0]
                comp._save_coloring(coloring)

                # now make a second problem to use the coloring
                _clear_problem_names()
                prob = Problem(name=f'test_partials_explicit_shape_bug_static_{method}')
                model = prob.model
                indeps = IndepVarComp()
                indeps.add_output('x0', np.ones(4))
                indeps.add_output('x1', np.ones(3))

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                comp.declare_coloring(wrt='x*', method=method)
                comp.use_fixed_coloring()
                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                comp._linearize()
                jac = comp._jacobian._subjacs_info
                _check_partial_matrix(comp, jac, sparsity, method)

    def test_simple_totals_static(self):
        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,19],
                                                        [1,2,11]):
            with self.subTest(msg=f'{method=}_{isplit=}_{osplit=}'):
                prob = Problem(name=f'test_simple_totals_static_{method}_{isplit}_{osplit}')
                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=osplit))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.comp.add_constraint('y%d' % i)

                model.approx_totals(method=method)
                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                # try just manually computing the coloring here instead of using declare_coloring
                coloring = compute_total_coloring(prob)
                model._save_coloring(coloring)

                # new Problem, loading the coloring we just computed
                _clear_problem_names()
                prob = Problem(name=f'test_simple_totals_static_{method}_{isplit}_{osplit}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=osplit))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.comp.add_constraint('y%d' % i)

                model.approx_totals(method=method)

                model.declare_coloring(wrt='*', method=method)
                model.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                derivs = prob.driver._compute_totals()

                nruns = comp._nruns - start_nruns
                self.assertEqual(nruns, 10)
                _check_total_matrix(model, derivs, sparsity, method)

    def test_static_totals_over_implicit_comp(self):
        for method, isplit, osplit in itertools.product(['fd', 'cs'],
                                                        [1,2,19],
                                                        [1,2,11]):
            with self.subTest(msg=f'{method=}_{isplit=}_{osplit=}'):
                prob = Problem(name=f'test_static_totals_over_implicit_comp_{method}_{isplit}_{osplit}')

                model = prob.model

                sparsity = setup_sparsity(_BIGMASK)
                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.nonlinear_solver = NonlinearBlockGS()
                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method, isplit=isplit, osplit=osplit))

                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.comp.add_constraint('y%d' % i)

                model.approx_totals(method=method)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                model._save_coloring(compute_total_coloring(prob))

                _clear_problem_names()
                prob = Problem(name=f'test_static_totals_over_implicit_comp_{method}_{isplit}_{osplit}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, _BIGMASK.shape[1], 'indeps', 'comp')

                model.nonlinear_solver = NonlinearBlockGS()
                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method, isplit=isplit, osplit=osplit))
                for conn in conns:
                    model.connect(*conn)

                for i in range(isplit):
                    model.add_design_var('indeps.x%d' % i)

                for i in range(osplit):
                    model.comp.add_constraint('y%d' % i)

                model.approx_totals(method=method)

                model.declare_coloring(wrt='*', method=method)
                model.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')

                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                derivs = prob.driver._compute_totals()  # colored

                nruns = comp._nruns - start_nruns
                # NLBGS ends up doing a single iteration after initialization, resulting in 2
                # runs per NL solve, so we multiplly the number of colored solvers by 2
                self.assertEqual(nruns, 10 * 2)
                _check_total_matrix(model, derivs, sparsity, method)

    def test_totals_of_indices(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_totals_of_indices_{method}')

                model = prob.model

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [1, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=2))
                for conn in conns:
                    model.connect(*conn)

                model.comp.add_constraint('y0', indices=[0,2])
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0')
                model.add_design_var('indeps.x1')
                model.approx_totals(method=method)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                model._save_coloring(compute_total_coloring(prob))

                _clear_problem_names()
                prob = Problem(name=f'test_totals_of_indices_{method}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_constraint('y0', indices=[0,2])
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0')
                model.add_design_var('indeps.x1')
                model.approx_totals(method=method)

                model.declare_coloring(wrt='*', method=method)
                model.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                derivs = prob.driver._compute_totals()  # colored

                nruns = comp._nruns - start_nruns
                self.assertEqual(nruns, 3)
                rows = [0,2,3,4]
                _check_total_matrix(model, derivs, sparsity[rows, :], method)

    def test_totals_wrt_indices(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_totals_wrt_indices_{method}')
                model = prob.model

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [1, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_constraint('y0')
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0', indices=[0,2])
                model.add_design_var('indeps.x1')
                model.approx_totals(method=method)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                model._save_coloring(compute_total_coloring(prob))

                _clear_problem_names()
                prob = Problem(name=f'test_totals_wrt_indices_{method}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                        isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_constraint('y0')
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0', indices=[0,2])
                model.add_design_var('indeps.x1')
                model.approx_totals(method=method)

                model.declare_coloring(wrt='*', method=method)
                model.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                derivs = prob.driver._compute_totals()  # colored

                nruns = comp._nruns - start_nruns
                # only 4 cols to solve for, but we get coloring of [[2],[3],[0,1]] so only 1 better
                self.assertEqual(nruns, 3)
                cols = [0,2,3,4]
                _check_total_matrix(model, derivs, sparsity[:, cols], method)

    def test_totals_of_wrt_indices(self):
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_totals_of_wrt_indices_{method}')
                model = prob.model

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1],
                    [1, 0, 0, 0, 0],
                    [0, 1, 1, 0, 0]]
                )

                isplit = 2
                sparsity = setup_sparsity(mask)
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                    isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_constraint('y0', indices=[0,2])
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0', indices=[0,2])
                model.add_design_var('indeps.x1')

                model.approx_totals(method=method)

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                model._save_coloring(compute_total_coloring(prob))

                _clear_problem_names()
                prob = Problem(name=f'test_totals_of_wrt_indices_{method}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                        isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'comp.x0')
                model.connect('indeps.x1', 'comp.x1')

                model.comp.add_constraint('y0', indices=[0,2])
                model.comp.add_constraint('y1')
                model.add_design_var('indeps.x0', indices=[0,2])
                model.add_design_var('indeps.x1')
                model.approx_totals(method=method)

                model.declare_coloring(wrt='*', method=method)
                model.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')

                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                derivs = prob.driver._compute_totals()  # colored

                nruns = comp._nruns - start_nruns
                self.assertEqual(nruns, 3)
                cols = rows = [0,2,3,4]
                _check_total_matrix(model, derivs, sparsity[rows, :][:, cols], method)



@unittest.skipUnless(MPI and PETScVector, "MPI and PETSc is required.")
@use_tempdirs
class TestStaticColoringParallelCS(unittest.TestCase):
    N_PROCS = 2

    def setUp(self):
        np.random.seed(11)

    # semi-total coloring feature disabled.

    def test_simple_semitotals_all_local_vars(self):
        raise self.skipTest('Semi-total coloring currently not supported.')
        for method in ['fd', 'cs']:
            with self.subTest(msg=f'{method=}'):
                prob = Problem(name=f'test_simple_semitotals_all_local_vars_{method}')

                model = prob.model

                mask = np.array(
                    [[1, 0, 0, 1, 1],
                        [0, 1, 0, 1, 1],
                        [0, 1, 0, 1, 1],
                        [1, 0, 0, 0, 0],
                        [0, 1, 1, 0, 0]]
                )
                if prob.comm.rank == 0:
                    sparsity = setup_sparsity(mask)
                    prob.comm.bcast(sparsity, root=0)
                else:
                    sparsity = prob.comm.bcast(None, root=0)

                isplit = 2
                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                sub = model.add_subsystem('sub', Group(num_par_fd=self.N_PROCS))
                sub.approx_totals(method=method)
                comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'sub.comp.x0')
                model.connect('indeps.x1', 'sub.comp.x1')

                model.sub.comp.add_constraint('y0')
                model.sub.comp.add_constraint('y1')
                model.add_design_var('indeps.x0')
                model.add_design_var('indeps.x1')

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()
                coloring = sub._compute_coloring(wrt_patterns='*', method=method)[0]
                sub._save_coloring(coloring)

                # make sure coloring file exists by the time we try to load the spec
                prob.comm.barrier()

                # now create a second problem and use the static coloring
                prob = Problem(name=f'test_simple_semitotals_all_local_vars_{method}')
                model = prob.model

                indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

                model.add_subsystem('indeps', indeps)
                sub = model.add_subsystem('sub', Group(num_par_fd=self.N_PROCS))
                #sub.approx_totals(method=method)
                comp = sub.add_subsystem('comp', SparseCompExplicit(sparsity, method, isplit=isplit, osplit=2))
                model.connect('indeps.x0', 'sub.comp.x0')
                model.connect('indeps.x1', 'sub.comp.x1')

                model.sub.comp.add_constraint('y0')
                model.sub.comp.add_constraint('y1')
                model.add_design_var('indeps.x0')
                model.add_design_var('indeps.x1')

                sub.declare_coloring(wrt='*', method=method)
                sub.use_fixed_coloring()

                prob.setup(check=False, mode='fwd')
                prob.set_solver_print(level=0)
                prob.run_model()

                start_nruns = comp._nruns
                #derivs = prob.driver._compute_totals()
                sub._linearize(sub._jacobian)
                nruns = comp._nruns - start_nruns
                if sub._full_comm is not None:
                    nruns = sub._full_comm.allreduce(nruns)

                _check_partial_matrix(sub, sub._jacobian._subjacs_info, sparsity, method)
                self.assertEqual(nruns, 3)

    @parameterized.expand(['fd', 'cs'], name_func=lambda func, n, p: f'{func.__name__}_{p.args[0]}')
    def test_simple_partials_implicit(self, method):
        prob = Problem(name=f'test_simple_partials_implicit_{method}')
        model = prob.model

        mask = np.array(
            [[1, 0, 0, 1, 1, 1, 0],
                [0, 1, 0, 1, 0, 1, 1],
                [0, 1, 0, 1, 1, 1, 0],
                [1, 0, 0, 0, 0, 1, 0],
                [0, 1, 1, 0, 1, 1, 1]]
        )

        if prob.comm.rank == 0:
            sparsity = setup_sparsity(mask)
            prob.comm.bcast(sparsity, root=0)
        else:
            sparsity = prob.comm.bcast(None, root=0)

        isplit = 2
        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method,
                                                            isplit=isplit, osplit=2,
                                                            num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)

        prob.run_model()
        coloring = comp._compute_coloring(wrt_patterns='x*', method=method)[0]

        comp._save_coloring(coloring)

        # make sure coloring file exists by the time we try to load the spec
        prob.comm.barrier()

        _clear_problem_names()
        prob = Problem(name=f'test_simple_partials_implicit_{method}')
        model = prob.model

        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompImplicit(sparsity, method,
                                                                isplit=isplit, osplit=2,
                                                                num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        comp.declare_coloring(wrt='x*', method=method)
        comp.use_fixed_coloring()

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()   # colored
        # number of runs = ncolors + number of outputs (only input columns were colored here)
        nruns = comp._nruns - start_nruns
        if comp._full_comm is not None:
            nruns = comp._full_comm.allreduce(nruns)
        self.assertEqual(nruns, 5 + sparsity.shape[0])

        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity, method)

    @parameterized.expand(['fd', 'cs'], name_func=lambda func, n, p: f'{func.__name__}_{p.args[0]}')
    def test_simple_partials_explicit(self, method):
        from openmdao.utils.reports_system import clear_report_registry
        prob = Problem(name=f'test_simple_partials_explicit_{method}')
        model = prob.model

        mask = np.array(
                [[1, 0, 0, 1, 1, 1, 0],
                    [0, 1, 0, 1, 0, 1, 1],
                    [0, 1, 0, 1, 1, 1, 0],
                    [1, 0, 0, 0, 0, 1, 0],
                    [0, 1, 1, 0, 1, 1, 1]]
            )

        if prob.comm.rank == 0:
            sparsity = setup_sparsity(mask)
            prob.comm.bcast(sparsity, root=0)
        else:
            sparsity = prob.comm.bcast(None, root=0)

        isplit = 2
        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                            isplit=isplit, osplit=2,
                                                            num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()
        coloring = comp._compute_coloring(wrt_patterns='x*', method=method)[0]
        comp._save_coloring(coloring)

        # make sure coloring file exists by the time we try to load the spec
        prob.comm.barrier()

        # now create a new problem and use the previously generated coloring
        _clear_problem_names()
        clear_report_registry()
        prob = Problem(name=f'test_simple_partials_explicit_{method}')
        model = prob.model

        indeps, conns = setup_indeps(isplit, mask.shape[1], 'indeps', 'comp')

        model.add_subsystem('indeps', indeps)
        comp = model.add_subsystem('comp', SparseCompExplicit(sparsity, method,
                                                                isplit=isplit, osplit=2,
                                                                num_par_fd=self.N_PROCS))
        model.connect('indeps.x0', 'comp.x0')
        model.connect('indeps.x1', 'comp.x1')

        comp.declare_coloring(wrt='x*', method=method)
        comp.use_fixed_coloring()
        prob.setup(check=False, mode='fwd')
        prob.set_solver_print(level=0)
        prob.run_model()

        start_nruns = comp._nruns
        comp._linearize()
        nruns = comp._nruns - start_nruns
        if comp._full_comm is not None:
            nruns = comp._full_comm.allreduce(nruns)
        self.assertEqual(nruns, 5)

        jac = comp._jacobian._subjacs_info
        _check_partial_matrix(comp, jac, sparsity, method)


if __name__ == '__main__':
    unittest.main()
