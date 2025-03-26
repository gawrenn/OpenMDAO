"""
Functions used for the display of derivatives matrices.
"""

from enum import Enum
import textwrap
from itertools import chain
from io import StringIO

import numpy as np

try:
    import rich
    # from rich.console import Console
except ImportError:
    rich is None

from openmdao.core.constants import _UNDEFINED
from openmdao.utils.array_utils import get_errors
from openmdao.utils.general_utils import add_border, is_undefined
from openmdao.utils.mpi import MPI
from openmdao.visualization.tables.table_builder import generate_table


class _Style(Enum):
    """
    Styles tags used in formatting output with rich.
    """
    ERR = 'bright_red'
    OUT_SPARSITY = 'dim'
    IN_SPARSITY = 'bold'
    WARN = 'orange1'
    SYSTEM = {'bold', 'blue'}
    VAR = {'bold', 'green'}


def rich_wrap(s, *tags):
    """
    If rich is available, wrap the given string in the provided tags.
    If rich is not available, just return the string.

    Parameters
    ----------
    s : str
        The string to be wrapped in rich tags.
    *tags : str
        The rich tags to be wrapped around s. These can either be
        strings, elements of the _Style enumeration, or sets/lists/tuples thereof.

    Returns
    -------
    str
        The given string wrapped in the provided rich tags.
    """
    if rich is None or not tags or not tags[0]:
        return s

    def flatten(lst):
        seq = list(chain.from_iterable(x if isinstance(x, (list, set, tuple))
                                       else [x] for x in lst))
        return seq

    cmds = sorted(flatten([t if isinstance(t, str) else t.value for t in tags]))
    on = ' '.join(cmds)
    off = '/' + ' '.join(reversed(cmds))
    return f'[{on}]{s}[{off}]'


def _deriv_display(system, err_iter, derivatives, rel_error_tol, abs_error_tol, out_stream,
                   fd_opts, totals=False, show_only_incorrect=False, lcons=None):
    """
    Print derivative error info to out_stream.

    Parameters
    ----------
    system : System
        The system for which derivatives are being displayed.
    err_iter : iterator
        Iterator that yields tuples of the form (key, fd_norm, fd_opts, directional, above_tol,
        inconsistent) for each subjac.
    derivatives : dict
        Dictionary containing derivative information keyed by (of, wrt).
    rel_error_tol : float
        Relative error tolerance.
    abs_error_tol : float
        Absolute error tolerance.
    out_stream : file-like object
            Where to send human readable output.
            Set to None to suppress.
    fd_opts : dict
        Dictionary containing options for the finite difference.
    totals : bool
        True if derivatives are totals.
    show_only_incorrect : bool, optional
        Set to True if output should print only the subjacs found to be incorrect.
    lcons : list or None
        For total derivatives only, list of outputs that are actually linear constraints.
    sort : bool
        If True, sort subjacobian keys alphabetically.
    """
    from openmdao.core.component import Component

    if out_stream is None:
        return

    # Match header to appropriate type.
    if isinstance(system, Component):
        sys_type = 'Component'
    else:
        sys_type = 'Group'

    sys_name = system.pathname
    sys_class_name = type(system).__name__

    if totals:
        sys_name = 'Full Model'

    num_bad_jacs = 0  # Keep track of number of bad derivative values for each component

    # Need to capture the output of a component's derivative
    # info so that it can be used if that component is the
    # worst subjac. That info is printed at the bottom of all the output
    sys_buffer = StringIO()

    if totals:
        title = "Total Derivatives"
    else:
        title = f"{sys_type}: {sys_class_name} '{sys_name}'"

    print(f"{add_border(title, '-')}\n", file=sys_buffer)
    parts = []

    for key, fd_opts, directional, above_tol, inconsistent in err_iter:

        if above_tol or inconsistent:
            num_bad_jacs += 1

        of, wrt = key
        derivative_info = derivatives[key]

        # Informative output for responses that were declared with an index.
        indices = derivative_info.get('indices')
        if indices is not None:
            of = f'{of} (index size: {indices})'

        # need this check because if directional may be list
        if isinstance(wrt, str):
            wrt = f"'{wrt}'"
        if isinstance(of, str):
            of = f"'{of}'"

        if directional:
            wrt = f"(d){wrt}"

        tol_violations = derivative_info['tol violation']
        abs_errs = derivative_info['abs error']
        rel_errs = derivative_info['rel error']
        vals_at_max_err = derivative_info['vals_at_max_error']
        steps = derivative_info['steps']

        Jfwd = derivative_info.get('J_fwd')
        Jrev = derivative_info.get('J_rev')

        if len(steps) > 1:
            stepstrs = [f", step={step}" for step in steps]
        else:
            stepstrs = [""]

        fd_desc = f"{fd_opts['method']}:{fd_opts['form']}"
        parts.append(f"  {sys_name}: {of} wrt {wrt}")
        if not isinstance(of, tuple) and lcons and of.strip("'") in lcons:
            parts[-1] += " (Linear constraint)"
        parts.append('')

        def tol_violation_str(check_str, desired_str):
            return f'({check_str} - {desired_str}) - (atol + rtol * {desired_str})'

        for i in range(len(tol_violations)):
            if directional:
                if totals and tol_violations[i].forward is not None:
                    err = _format_error(tol_violations[i].forward, 0.0)
                    parts.append(f'    Max Tolerance Violation ([fwd, fd] Dot Product Test)'
                                 f'{stepstrs[i]} : {err}')
                    parts.append(f'      abs error: {abs_errs[i].forward:.6e}')
                    parts.append(f'      rel error: {rel_errs[i].forward:.6e}')
                    parts.append(f'      fwd value: {vals_at_max_err[i].forward[0]:.6e}')
                    parts.append(f'      fd value: {vals_at_max_err[i].forward[1]:.6e} '
                                 f'({fd_desc}{stepstrs[i]})\n')

                if ('directional_fd_rev' in derivative_info and
                        derivative_info['directional_fd_rev'][i]):
                    err = _format_error(tol_violations[i].reverse, 0.0)
                    parts.append(f'    Max Tolerance Violation ([rev, fd] Dot Product Test)'
                                 f'{stepstrs[i]} : {err}')
                    parts.append(f'      abs error: {abs_errs[i].reverse:.6e}')
                    parts.append(f'      rel error: {rel_errs[i].reverse:.6e}')
                    fd, rev = derivative_info['directional_fd_rev'][i]
                    parts.append(f'      rev value: {rev:.6e}')
                    parts.append(f'      fd value: {fd:.6e} ({fd_desc}{stepstrs[i]})\n')
            else:
                if tol_violations[i].forward is not None:
                    err = _format_error(tol_violations[i].forward, 0.0)
                    parts.append(f'    Max Tolerance Violation {tol_violation_str("Jfwd", "Jfd")}'
                                 f'{stepstrs[i]} : {err}')
                    parts.append(f'      abs error: {abs_errs[i].forward:.6e}')
                    parts.append(f'      rel error: {rel_errs[i].forward:.6e}')
                    parts.append(f'      fwd value: {vals_at_max_err[i].forward[0]:.6e}')
                    parts.append(f'      fd value: {vals_at_max_err[i].forward[1]:.6e} '
                                 f'({fd_desc}{stepstrs[i]})\n')

                if tol_violations[i].reverse is not None:
                    err = _format_error(tol_violations[i].reverse, 0.0)
                    parts.append(f'    Max Tolerance Violation {tol_violation_str("Jrev", "Jfd")}'
                                 f'{stepstrs[i]} : {err}')
                    parts.append(f'      abs error: {abs_errs[i].reverse:.6e}')
                    parts.append(f'      rel error: {rel_errs[i].reverse:.6e}')
                    parts.append(f'      rev value: {vals_at_max_err[i].reverse[0]:.6e}')
                    parts.append(f'      fd value: {vals_at_max_err[i].reverse[1]:.6e} '
                                 f'({fd_desc}{stepstrs[i]})\n')

        if directional:
            if ('directional_fwd_rev' in derivative_info and
                    derivative_info['directional_fwd_rev']):
                err = _format_error(tol_violations[0].fwd_rev, 0.0)
                parts.append(f'    Max Tolerance Violation ([rev, fwd] Dot Product Test) : {err}')
                parts.append(f'      abs error: {abs_errs[0].fwd_rev:.6e}')
                parts.append(f'      rel error: {rel_errs[0].fwd_rev:.6e}')
                fwd, rev = derivative_info['directional_fwd_rev']
                parts.append(f'      rev value: {rev:.6e}')
                parts.append(f'      fwd value: {fwd:.6e}\n')
        elif tol_violations[0].fwd_rev is not None:
            err = _format_error(tol_violations[0].fwd_rev, 0.0)
            parts.append(f'    Max Tolerance Violation {tol_violation_str("Jrev", "Jfwd")}'
                         f' : {err}')
            parts.append(f'      abs error: {abs_errs[0].fwd_rev:.6e}')
            parts.append(f'      rel error: {rel_errs[0].fwd_rev:.6e}')
            parts.append(f'      rev value: {vals_at_max_err[0].fwd_rev[0]:.6e}')
            parts.append(f'      fwd value: {vals_at_max_err[0].fwd_rev[1]:.6e}\n')

        if inconsistent:
            parts.append('\n    * Inconsistent value across ranks *\n')

        comm = system._problem_meta['comm']
        if MPI and comm.size > 1:
            parts.append(f'\n    MPI Rank {comm.rank}\n')

        if 'uncovered_nz' in derivative_info:
            uncovered_nz = list(derivative_info['uncovered_nz'])
            uncovered_threshold = derivative_info['uncovered_threshold']
            rs = np.array([r for r, _ in uncovered_nz], dtype=int)
            cs = np.array([c for _, c in uncovered_nz])
            parts.append(f'    Sparsity excludes {len(uncovered_nz)} entries which'
                         f' appear to be non-zero. (Magnitudes exceed {uncovered_threshold}) *')
            with np.printoptions(linewidth=1000, formatter={'int': lambda i: f'{i}'}):
                parts.append(f'      Rows: {rs}')
                parts.append(f'      Cols: {cs}\n')

        with np.printoptions(linewidth=240):
            # Raw Derivatives
            if tol_violations[0].forward is not None:
                if directional:
                    parts.append('    Directional Derivative (Jfwd)')
                else:
                    parts.append('    Raw Forward Derivative (Jfwd)')
                Jstr = textwrap.indent(str(Jfwd), '    ')
                parts.append(f"{Jstr}\n")

            fdtype = fd_opts['method'].upper()

            if tol_violations[0].reverse is not None:
                if directional:
                    if totals:
                        parts.append('    Directional Derivative (Jrev) Dot Product')
                    else:
                        parts.append('    Directional Derivative (Jrev)')
                else:
                    parts.append('    Raw Reverse Derivative (Jrev)')
                Jstr = textwrap.indent(str(Jrev), '    ')
                parts.append(f"{Jstr}\n")

            try:
                fds = derivative_info['J_fd']
            except KeyError:
                fds = [0.]

            for i in range(len(tol_violations)):
                fd = fds[i]

                Jstr = textwrap.indent(str(fd), '    ')
                if directional:
                    if totals and tol_violations[i].reverse is not None:
                        parts.append(f'    Directional {fdtype} Derivative (Jfd) '
                                     f'Dot Product{stepstrs[i]}\n{Jstr}\n')
                    else:
                        parts.append(f"    Directional {fdtype} Derivative (Jfd)"
                                     f"{stepstrs[i]}\n{Jstr}\n")
                else:
                    parts.append(f"    Raw {fdtype} Derivative (Jfd){stepstrs[i]}"
                                 f"\n{Jstr}\n")

        parts.append(' -' * 30)
        parts.append('')

    sys_buffer.write('\n'.join(parts))

    if not show_only_incorrect or num_bad_jacs > 0:
        out_stream.write(sys_buffer.getvalue())


def _print_tv(tol_violation):
    """
    Enclose the tolerance violation in parentheses if it is negative.

    Parameters
    ----------
    tol_violation : float
        The tolerance violation.

    Returns
    -------
    str
        The formatted tolerance violation.
    """
    if tol_violation < 0:
        return f'({tol_violation:.6e})'
    return f'{tol_violation:.6e}'


def _deriv_display_compact(system, err_iter, derivatives, out_stream, totals=False,
                           show_only_incorrect=False, show_worst=False):
    """
    Print derivative error info to out_stream in a compact tabular format.

    Parameters
    ----------
    system : System
        The system for which derivatives are being displayed.
    err_iter : iterator
        Iterator that yields tuples of the form (key, fd_norm, fd_opts, directional, above_tol,
        inconsistent) for each subjac.
    derivatives : dict
        Dictionary containing derivative information keyed by (of, wrt).
    out_stream : file-like object
            Where to send human readable output.
            Set to None to suppress.
    totals : bool
        True if derivatives are totals.
    show_only_incorrect : bool, optional
        Set to True if output should print only the subjacs found to be incorrect.
    show_worst : bool
        Set to True to show the worst subjac.

    Returns
    -------
    tuple or None
        Tuple contains the worst tolerance violation, corresponding table row, and table header.
    """
    if out_stream is None:
        return

    from openmdao.core.component import Component

    # Match header to appropriate type.
    if isinstance(system, Component):
        sys_type = 'Component'
    else:
        sys_type = 'Group'

    sys_name = system.pathname
    sys_class_name = type(system).__name__
    matrix_free = system.matrix_free and not totals

    if totals:
        sys_name = 'Full Model'

    num_bad_jacs = 0  # Keep track of number of bad derivative values for each component

    # Need to capture the output of a component's derivative
    # info so that it can be used if that component is the
    # worst subjac. That info is printed at the bottom of all the output
    sys_buffer = StringIO()

    if totals:
        title = "Total Derivatives"
    else:
        title = f"{sys_type}: {sys_class_name} '{sys_name}'"

    print(f"{add_border(title, '-')}\n", file=sys_buffer)

    table_data = []
    worst_subjac = None

    for key, _, directional, above_tol, inconsistent in err_iter:

        if above_tol or inconsistent:
            num_bad_jacs += 1

        of, wrt = key
        derivative_info = derivatives[key]

        # Informative output for responses that were declared with an index.
        indices = derivative_info.get('indices')
        if indices is not None:
            of = f'{of} (index size: {indices})'

        if directional:
            wrt = f"(d) {wrt}"

        tol_violations = derivative_info['tol violation']
        vals_at_max_err = derivative_info['vals_at_max_error']
        steps = derivative_info['steps']

        # loop over different fd step sizes
        for tol_violation, abs_val, step in zip(tol_violations, vals_at_max_err, steps):

            err_desc = []
            maxtv = tol_violation.max(use_abs=False)
            if maxtv > 0.:
                err_desc.append(f'{maxtv: .6e}>TOL')
            if inconsistent:
                err_desc.append(' <RANK INCONSISTENT>')
            if 'uncovered_nz' in derivative_info:
                err_desc.append(' <BAD SPARSITY>')
            err_desc = ''.join(err_desc)

            start = [of, wrt, step] if len(steps) > 1 else [of, wrt]

            if totals:
                # use forward even if both fwd and rev are defined
                if tol_violation.forward is not None:
                    calc_abs = _print_tv(tol_violation.forward)
                    calc_abs_val_fd = abs_val.forward[1]
                    calc_abs_val = abs_val.forward[0]
                elif tol_violation.reverse is not None:
                    calc_abs = _print_tv(tol_violation.reverse)
                    calc_abs_val_fd = abs_val.reverse[1]
                    calc_abs_val = abs_val.reverse[0]

                table_data.append(start + [calc_abs_val, calc_abs_val_fd, calc_abs, err_desc])
            else:  # partials
                if matrix_free:
                    table_data.append(start +
                                      [abs_val.forward[0], abs_val.forward[1],
                                       _print_tv(tol_violation.forward),
                                       abs_val.reverse[0], abs_val.reverse[1],
                                       _print_tv(tol_violation.reverse),
                                       abs_val.fwd_rev[0], abs_val.fwd_rev[1],
                                       _print_tv(tol_violation.fwd_rev),
                                       err_desc])
                else:
                    if abs_val.forward is not None:
                        table_data.append(start +
                                          [abs_val.forward[0], abs_val.forward[1],
                                           _print_tv(tol_violation.forward), err_desc])
                    else:
                        table_data.append(start +
                                          [abs_val.reverse[0], abs_val.reverse[1],
                                           _print_tv(tol_violation.reverse), err_desc])

                # See if this subjacobian has the greater error in the derivative computation
                # compared to the other subjacobians so far
                if worst_subjac is None or tol_violation.max(use_abs=False) > worst_subjac[0]:
                    worst_subjac = (tol_violation.max(use_abs=False), table_data[-1])

    headers = []
    if table_data:
        headers = ["'of' variable", "'wrt' variable"]
        if len(steps) > 1:
            headers.append('step')

        column_meta = {}

        if matrix_free:
            column_meta[4] = {'align': 'right'}
            column_meta[7] = {'align': 'right'}
            column_meta[10] = {'align': 'right'}
            headers.extend(['fwd val', 'fd val', '(fwd-fd) - (a + r*fd)',
                            'rev val', 'fd val', '(rev-fd) - (a + r*fd)',
                            'fwd val', 'rev val', '(fwd-rev) - (a + r*rev)',
                            'error desc'])
        else:
            column_meta[4] = {'align': 'right'}
            headers.extend(['calc val', 'fd val', '(calc-fd) - (a + r*fd)',
                            'error desc'])

        _print_deriv_table(table_data, headers, sys_buffer, col_meta=column_meta)

        if worst_subjac is not None and worst_subjac[0] <= 0:
            worst_subjac = None

        if show_worst and worst_subjac is not None:
            if worst_subjac[0] > 0:
                print(f"\nWorst Sub-Jacobian (tolerance violation): {worst_subjac[0]}\n",
                      file=sys_buffer)
                _print_deriv_table([worst_subjac[1]], headers, sys_buffer, col_meta=column_meta)

    if not show_only_incorrect or num_bad_jacs > 0:
        out_stream.write(sys_buffer.getvalue())

    if worst_subjac is None:
        return None

    return worst_subjac + (headers, column_meta)


def _format_error(error, tol):
    """
    Format the error, flagging if necessary.

    Parameters
    ----------
    error : float
        The error.
    tol : float
        Tolerance above which errors are flagged

    Returns
    -------
    str
        Formatted and possibly flagged error.
    """
    if np.isnan(error) or error < tol:
        return f'({error:.6e})'
    return f'{error:.6e} *'


def _print_deriv_table(table_data, headers, out_stream, tablefmt='grid', col_meta=None):
    """
    Print a table of derivatives.

    Parameters
    ----------
    table_data : list
        List of lists containing the table data.
    headers : list
        List of column headers.
    out_stream : file-like object
        Where to send human readable output.
        Set to None to suppress.
    tablefmt : str
        The table format to use.
    col_meta : dict
        Dict containing metadata keyed by column index.
    """
    if table_data and out_stream is not None:
        num_col_meta = {'format': '{: .6e}'}
        column_meta = [{}, {}]
        column_meta.extend([num_col_meta.copy() for _ in range(len(headers) - 3)])
        column_meta.append({})
        if col_meta:
            for i, meta in col_meta.items():
                column_meta[i].update(meta)

        print(generate_table(table_data, headers=headers, tablefmt=tablefmt,
                             column_meta=column_meta, missing_val='n/a'), file=out_stream)


class _JacFormatter:
    """
    A class

    Parameters
    ----------
    shape : tuple
        The shape of the jacobian matrix being printed.
    nzrows : array-like or None
        The nonzero rows in the sparsity pattern.
    nzcols : array-like or None
        The nonzero columns in the sparsity pattern.
    Jref : array-like or None
        A reference jacobian with which any values are checked for error.
    abs_err_tol : float
        The absolute error tolerance to signify errors in the element being printed.
    rel_err_tol : float
        The relative error tolerance to signify errors in the element being printed.
    show_uncovered : bool
        If True, highlight nonzero elements outside of the given sparsity pattern
        as erroneous.

    Attributes
    ----------
    _shape : tuple[int]
        Thes hape of the jacobian matrix being printed.
    _nonzero : array-like or None
        The nonzero rows and columns in the sparsity pattern.
    _Jref : array-like or None
        A reference jacobian with which any values are checked for error.
    _abs_err_tol : float
        The absolute error tolerance to signify errors in the element being printed.
    _rel_err_tol : float
        The relative error tolerance to signify errors in the element being printed.
    _show_uncovered : bool
        If True, highlight nonzero elements outside of the given sparsity pattern
        as erroneous.
    _uncovered_nz : list or None
        If given, the coordinates of the uncovered nonzeros in the sparsity pattern.
    _i : int
        An internal counter used to track the current row being printed.
    _j : int
        An internal counter used to track the current column being printed.
    """
    def __init__(self, shape, nzrows=None, nzcols=None, Jref=None,
                 abs_err_tol=1.0E-8, rel_err_tol=1.0E-8, uncovered=None):
        self._shape = shape

        if nzrows is not None and nzcols is not None:
            self._nonzero = list(zip(nzrows, nzcols))
        else:
            self._nonzero = None

        self._Jref = Jref

        self._abs_err_tol = abs_err_tol
        self._rel_err_tol = rel_err_tol

        self._uncovered = uncovered

        # _i and _j are used to track the current row/col being printed.
        self._i = 0
        self._j = 0

    def reset(self, Jref=_UNDEFINED):
        """
        Reset the row/column counters, and optionally provide
        a new reference jacobian for error calculation.

        Parameters
        ----------
        Jref : array-like
            A reference jacobian against any values are checked for error.
        """
        self._i = 0
        self._j = 0
        if not is_undefined(Jref):
            self._Jref = Jref

    def __call__(self, x):
        """
        Return a formatted version of element x when printing a numpy array.

        If the rich package is available, wrap the formatted x in
        rich tags to denote
        - whether the element is included in the sparsity pattern
        - whether the element causes a violation of the derivative check

        Parameters
        ----------
        x : float or int
            An element of a numpy array to be printed.

        Returns
        -------
        str
            The formatted version of the array element.
        """
        i, j = self._i, self._j
        Jref = self._Jref
        atol = self._abs_err_tol
        rtol = self._rel_err_tol

        has_sparsity = self._nonzero is not None

        # Default output, no format.
        s = f'{x: .6e}'

        if rich is not None:
            rich_fmt = set()
            if (Jref is not None and atol is not None and rtol is not None):
                abs_err, _, rel_err, _, _ = get_errors(x, Jref[i, j])
            else:
                abs_err = 0.0
                rel_err = 0.0

            if has_sparsity:
                if (i, j) in self._nonzero:
                    rich_fmt |= {_Style.IN_SPARSITY}
                    if abs_err > atol:
                        rich_fmt |= {_Style.ERR}
                    elif np.abs(x) == 0:
                        rich_fmt |= {_Style.WARN}
                else:
                    rich_fmt |= {_Style.OUT_SPARSITY}
                    if abs_err > atol:
                        rich_fmt |= {_Style.ERR}
                    elif self._uncovered is not None and (i, j) in self._uncovered:
                        rich_fmt |= {_Style.WARN}
            else:
                if abs_err > atol:
                    rich_fmt |= {_Style.ERR}
                elif rel_err > rtol:
                    rich_fmt |= {_Style.ERR}

            s = rich_wrap(s, *rich_fmt)

            # Increment the row and column being printed.
            self._j += 1
            if self._j >= self._shape[1]:
                self._j = 0
                self._i += 1
        return s
