"""
Utils for dealing with arrays.
"""
import sys
from itertools import product
import hashlib

import numpy as np

from scipy.sparse import coo_matrix, csr_matrix, issparse
from openmdao.core.constants import INT_DTYPE
from openmdao.utils.omnumba import numba


if sys.version_info >= (3, 8):
    from math import prod

    def shape_to_len(shape):
        """
        Compute length given a shape tuple.

        Parameters
        ----------
        shape : tuple of int or None
            Numpy shape tuple.

        Returns
        -------
        int
            Length of array.
        """
        if shape is None:
            return None
        return prod(shape)
else:
    def shape_to_len(shape):
        """
        Compute length given a shape tuple.

        For realistic-dimension arrays, looping over the shape tuple is much faster than np.prod.

        Parameters
        ----------
        shape : tuple of int
            Numpy shape tuple.

        Returns
        -------
        int
            Length of multidimensional array.
        """
        if shape is None:
            return None

        length = 1
        for dim in shape:
            length *= dim

        return length


def evenly_distrib_idxs(num_divisions, arr_size):
    """
    Return evenly distributed entries for the given array size.

    Given a number of divisions and the size of an array, chop the array up
    into pieces according to number of divisions, keeping the distribution
    of entries as even as possible.

    Parameters
    ----------
    num_divisions : int
        Number of parts to divide the array into.
    arr_size : int
        Number of entries in the array.

    Returns
    -------
    tuple
        A tuple of (sizes, offsets), where sizes and offsets contain values for all
        divisions.
    """
    base, leftover = divmod(arr_size, num_divisions)
    sizes = np.full(num_divisions, base, dtype=INT_DTYPE)

    # evenly distribute the remainder across size-leftover procs,
    # instead of giving the whole remainder to one proc
    sizes[:leftover] += 1

    offsets = np.zeros(num_divisions, dtype=INT_DTYPE)
    offsets[1:] = np.cumsum(sizes)[:-1]

    return sizes, offsets


def scatter_dist_to_local(dist_val, comm, sizes):
    """
    Scatter a full distributed value to local values in each MPI process.

    Parameters
    ----------
    dist_val : ndarray
        The full distributed value.
    comm : MPI communicator
        The MPI communicator.
    sizes : ndarray
        The array of sizes for each process.

    Returns
    -------
    ndarray
        The local value on this process.
    """
    from openmdao.utils.mpi import MPI
    offsets = np.zeros(sizes.shape, dtype=INT_DTYPE)
    offsets[1:] = np.cumsum(sizes)[:-1]
    local = np.zeros(sizes[comm.rank])
    comm.Scatterv([dist_val, sizes, offsets, MPI.DOUBLE], local, root=0)
    return local


def get_evenly_distributed_size(comm, full_size):
    """
    Return the size of the current rank's part of an array of the given size.

    Given a communicator and the size of an array, chop the array up
    into pieces according to the size of the communicator, keeping the
    distribution of entries as even as possible.

    Parameters
    ----------
    comm : MPI communicator
        The communicator we're distributing the array across.
    full_size : int
        Number of entries in the array.

    Returns
    -------
    int
        The size of this rank's part of the full distributed array.
    """
    base, leftover = divmod(full_size, comm.size)
    sizes = np.full(comm.size, base, dtype=INT_DTYPE)

    # evenly distribute the remainder across full_size-leftover procs,
    # instead of giving the whole remainder to one proc
    sizes[:leftover] += 1

    return sizes[comm.rank]


def take_nth(rank, size, seq):
    """
    Iterate returning every nth value.

    Return an iterator over the sequence that returns every
    nth element of seq based on the given rank within a group of
    the given size.  For example, if size = 2, a rank of 0 returns
    even indexed elements and a rank of 1 returns odd indexed elements.

    Parameters
    ----------
    rank : int
        MPI rank of this process.
    size : int
        Size of the array we're taking nth entries from.
    seq : iter
        Iterator containing the values being returned.

    Yields
    ------
    generator
    """
    assert rank < size
    it = iter(seq)
    while True:
        for proc in range(size):
            if rank == proc:
                try:
                    yield next(it)
                except StopIteration:
                    return
            else:
                try:
                    next(it)
                except StopIteration:
                    return


def csr_array_viz(arr, val_map=None, stream=sys.stdout):
    """
    Display the structure of a boolean array in a compact form.

    Parameters
    ----------
    arr : ndarray
        Array being visualized.
    val_map : dict or None
        Mapping of array values to characters.
    stream : file-like
        Stream where output will be written.
    """
    if len(arr.shape) != 2:
        raise RuntimeError("simple_array_viz only works for 2d arrays.")

    if val_map is None:
        val_map = {1: 'x', 0: '.'}

    final = arr.tocsr() if issparse(arr) else csr_matrix(arr, dtype=np.int8)
    final = final.astype(np.int8, copy=final.dtype is not np.int8)
    rowarr = np.zeros(final.shape[1], dtype=np.int8)

    for r in range(final.shape[0]):
        row = final.getrow(r)
        rowinds = row.indices
        rowarr[:] = 0
        rowarr[rowinds] = row.data
        stream.write(''.join(val_map[c] for c in rowarr))
        stream.write(f'  {r}\n')


def get_sparsity_diff_array(sparsity1, sparsity2):
    """
    Return an array showing the difference between two sparsity patterns.

    Parameters
    ----------
    sparsity1 : bool ndarray or sparse array or None
        First sparsity pattern.
    sparsity2 : bool ndarray or sparse array or None
        Second sparsity pattern.

    Returns
    -------
    csr_array
        Sparse array of dtype int8 where:
        0: zero val in both
        1: non-zero val in sparsity1
        3: non-zero val in sparsity2
        4: non-zero val in both.
    """
    assert not (sparsity1 is None and sparsity2 is None), \
        'At least one sparsity pattern must be provided.'
    if ((sparsity1 is not None and sparsity1.dtype != bool) or
            (sparsity2 is not None and sparsity2.dtype != bool)):
        raise ValueError('Sparsity patterns must be boolean.')

    if issparse(sparsity1):
        sp1 = sparsity1.tocsr().astype(np.int8)
    elif sparsity1 is None:
        sp1 = csr_matrix(([], ([], [])), shape=sparsity2.shape, dtype=np.int8)
    else:
        sp1 = csr_matrix(sparsity1, dtype=np.int8)

    if issparse(sparsity2):
        sp2 = sparsity2.tocsr().astype(np.int8)
    elif sparsity2 is None:  # build empty sparse matrix of same shape as sp1
        sp2 = csr_matrix(([], ([], [])), shape=sp1.shape, dtype=np.int8)
    else:
        sp2 = csr_matrix(sparsity2, dtype=np.int8)

    assert sp1.shape == sp2.shape, 'Sparsity patterns must have the same shape.'

    # set so that we get unique values for their sum:
    sp1.data[:] = 1
    sp2.data[:] = 3

    return sp1 + sp2


def sparsity_diff_viz(arr1, arr2, val_map=None, stream=sys.stdout):
    """
    Display the difference between two sparsity patterns in a compact form.

    Parameters
    ----------
    arr1 : ndarray
        First sparsity pattern.
    arr2 : ndarray
        Second sparsity pattern.
    val_map : dict or None
        Mapping of array values to characters.
    stream : file-like
        Stream where output will be written.

    Returns
    -------
    bool
        True if they agree, False otherwise.
    """
    if val_map is None:
        val_map = {0: '.', 1: '1', 3: '2', 4: 'x'}
    spdiff = get_sparsity_diff_array(arr1, arr2)
    csr_array_viz(spdiff, val_map=val_map, stream=stream)

    return 1 not in spdiff.data and 3 not in spdiff.data


def array_viz(arr, prob=None, of=None, wrt=None, stream=sys.stdout):
    """
    Display the structure of a boolean array in a compact form.

    If prob, of, and wrt are supplied, print the name of the response alongside
    each row and print the names of the design vars, aligned with each column, at
    the bottom.

    Parameters
    ----------
    arr : ndarray
        Array being visualized.
    prob : Problem or None
        Problem object.
    of : list of str or None
        Names of response variables used in derivative calculation.
    wrt : list of str or None
        Names of design variables used in derivative calculation.
    stream : file-like
        Stream where output will be written.
    """
    if len(arr.shape) != 2:
        raise RuntimeError("array_viz only works for 2d arrays.")

    if prob is not None:
        if of is None:
            of = prob.driver._get_ordered_nl_responses()
        if wrt is None:
            wrt = list(prob.driver._designvars)

    if prob is None or of is None or wrt is None:
        csr_array_viz(arr, stream=stream)
    else:
        row = 0
        for res in of:
            for r in range(row, row + prob.driver._responses[res]['size']):
                col = 0
                for dv in wrt:
                    for c in range(col, col + prob.driver._designvars[dv]['size']):
                        if arr[r, c]:
                            stream.write('x')
                        else:
                            stream.write('.')
                    col = c + 1
                stream.write(' %d  %s\n' % (r, res))
            row = r + 1

        start = 0
        for name in wrt:
            tab = ' ' * start
            stream.write('%s|%s\n' % (tab, name))
            start += prob.driver._designvars[name]['size']


def array_connection_compatible(shape1, shape2):
    """
    Return True if the two arrays shapes are compatible.

    Array shapes are compatible if the underlying data has the same size and is
    stored in the same contiguous order for the two shapes.

    Parameters
    ----------
    shape1 : tuple of int
        Shape of the first array.
    shape2 : tuple of int
        Shape of the second array.

    Returns
    -------
    bool
        True if the two shapes are compatible for connection, else False.
    """
    ashape1 = np.asarray(shape1, dtype=INT_DTYPE)
    ashape2 = np.asarray(shape2, dtype=INT_DTYPE)

    size1 = shape_to_len(ashape1)
    size2 = shape_to_len(ashape2)

    # Shapes are not connection-compatible if size is different
    if size1 != size2:
        return False

    nz1 = np.where(ashape1 > 1)[0]
    nz2 = np.where(ashape2 > 1)[0]

    if len(nz1) > 0:
        fundamental_shape1 = ashape1[np.min(nz1): np.max(nz1) + 1]
    else:
        fundamental_shape1 = np.ones((1,))

    if len(nz2) > 0:
        fundamental_shape2 = ashape2[np.min(nz2): np.max(nz2) + 1]
    else:
        fundamental_shape2 = np.ones((1,))

    if len(fundamental_shape1) != len(fundamental_shape2):
        return False

    return np.all(fundamental_shape1 == fundamental_shape2)


def tile_sparse_jac(data, rows, cols, nrow, ncol, num_nodes):
    """
    Assemble arrays necessary to define a COO sparse jacobian for a vectorized component.

    These arrays can also be passed to csc_matrix or csr_matrix to create CSC and CSR sparse
    matrices.

    Parameters
    ----------
    data : ndarray
        Array of values.
    rows : index array
        Array of row indices.
    cols : index array
        Array of column indices.
    nrow : int
        Number of rows in sub jacobian.
    ncol : int
        Number of columns in sub jacobian.
    num_nodes : int
        Number of vectorized copies to tile.

    Returns
    -------
    ndarray, ndarray, ndarray
        Arrays to define a COO sparse jacobian of size num_nodes*nrow by num_nodes*ncol.
    """
    nnz = len(rows)

    if np.ndim(data) == 0:
        data = data * np.ones(nnz)

    if np.ndim(nrow) > 0:
        nrow = shape_to_len(nrow)

    if np.ndim(ncol) > 0:
        ncol = shape_to_len(ncol)

    repeat_arr = np.repeat(np.arange(num_nodes), nnz)

    data = np.tile(data, num_nodes)
    rows = np.tile(rows, num_nodes) + repeat_arr * nrow
    cols = np.tile(cols, num_nodes) + repeat_arr * ncol

    return data, rows, cols


def _global2local_offsets(global_offsets):
    """
    Given existing global offsets, return a copy with offsets localized to each process.

    Parameters
    ----------
    global_offsets : dict
        Arrays of global offsets keyed by vec_name and deriv direction.

    Returns
    -------
    dict
        Arrays of local offsets keyed by vec_name and deriv direction.
    """
    offsets = {}
    for type_ in global_offsets:
        goff = global_offsets[type_]
        offsets[type_] = goff.copy()
        if goff[0].size > 0:
            # adjust offsets to be local in each process
            offsets[type_] -= goff[:, 0].reshape((goff.shape[0], 1))

    return offsets


def get_input_idx_split(full_idxs, inputs, outputs, use_full_cols, is_total):
    """
    Split an array of indices into vec outs + ins into two arrays of indices into outs and ins.

    Parameters
    ----------
    full_idxs : ndarray
        Indices into the full array (which could be outs + ins or just ins).
    inputs : Vector
        Inputs vector.
    outputs : Vector
        Outputs vector.
    use_full_cols : bool
        If True,  full idxs are into the full outs + ins vector.
    is_total : bool
        If True, total derivatives are being computed and wrt vector is the outputs vector.

    Returns
    -------
    list of tuples
        Each tuple is of the form (array, idxs).
    """
    assert len(full_idxs) > 0, "Empty index array passed to get_input_idx_split."
    full_idxs = np.asarray(full_idxs)
    if use_full_cols:
        out_size = len(outputs)
        out_idxs = full_idxs[full_idxs < out_size]
        in_idxs = full_idxs[full_idxs >= out_size] - out_size
        full = [(outputs, out_idxs), (inputs, in_idxs)]
        return [(vec, inds) for vec, inds in full if inds.size > 0]
    elif is_total:
        return [(outputs, full_idxs)]
    else:
        return [(inputs, full_idxs)]


def convert_neg(arr, size):
    """
    Convert negative indices based on full array size.

    Parameters
    ----------
    arr : ndarray
        The index array.
    size : int
        The full size of the array.

    Returns
    -------
    ndarray
        The array with negative indices converted to positive.
    """
    arr[arr < 0] += size
    return arr


def _flatten_src_indices(src_indices, shape_in, shape_out, size_out):
    """
    Convert src_indices into a flat, non-negative form.

    Parameters
    ----------
    src_indices : ndarray
        Array of src_indices.  Can be flat or multi-dimensional.
    shape_in : tuple
        Shape of the input variable.
    shape_out : tuple
        Shape of the output variable.
    size_out : int
        Size of the output variable.

    Returns
    -------
    ndarray
        The flattened src_indices.
    """
    if len(shape_out) == 1 or shape_in == src_indices.shape:
        return convert_neg(src_indices.ravel(), size_out)

    entries = [list(range(x)) for x in shape_in]
    cols = np.vstack([src_indices[i] for i in product(*entries)])
    dimidxs = [convert_neg(cols[:, i], shape_out[i]) for i in range(cols.shape[1])]
    return np.ravel_multi_index(dimidxs, shape_out)


def sizes2offsets(size_array):
    """
    For a given array of sizes, return an array of offsets.

    Offsets will be computed using a flattened version of size_array and then
    reshaped to match the shape of size_array.

    Parameters
    ----------
    size_array : ndarray
        Array of sizes.

    Returns
    -------
    ndarray
        Array of offsets.
    """
    offsets = np.zeros(size_array.size, dtype=size_array.dtype)
    offsets[1:] = np.cumsum(size_array.flat)[:-1]
    return offsets.reshape(size_array.shape)


def abs_complex(x):
    """
    Compute the absolute value of a complex-stepped vector.

    Rather than taking a Euclidian norm, simply negate the values that are less than zero.

    Parameters
    ----------
    x : ndarray
        Input array.

    Returns
    -------
    ndarray
        Complex-step absolute value of the array.
    """
    idx_neg = np.where(x < 0)
    x[idx_neg] = -x[idx_neg]
    return x


def dv_abs_complex(x, x_deriv):
    """
    Compute the complex-step derivative of the absolute value function and its derivative.

    Parameters
    ----------
    x : ndarray
        Input array, used for determining which elements to negate.
    x_deriv : ndarray
        Incominng partial derivative array, may have one additional dimension.

    Returns
    -------
    ndarray
        Absolute value applied to x.
    ndarray
        Absolute value applied to x_deriv.
    """
    idx_neg = np.where(x < 0)

    # Special case when x is (1, ) and x_deriv is (1, n).
    if len(x_deriv.shape) == 1:
        if idx_neg[0].size != 0:
            return -x, -x_deriv

    x[idx_neg] = -x[idx_neg]
    x_deriv[idx_neg] = -x_deriv[idx_neg]

    return x, x_deriv


def rand_sparsity(shape, density_ratio, dtype=bool, rng=None):
    """
    Return a random COO matrix of the given shape with given percent density.

    Row and column indices are generated using random integers so some duplication
    is possible, resulting in a matrix with somewhat lower density than specified.

    Parameters
    ----------
    shape : tuple
        Desired shape of the matrix.
    density_ratio : float
        Approximate ratio of nonzero to zero entries in the desired matrix.
    dtype : type
        Specifies type of the values in the returned matrix.
    rng : np.random.Generator or None
        Random number generator.

    Returns
    -------
    coo_matrix
        A COO matrix with approximately the nonzero density desired.
    """
    assert len(shape) == 2, f"shape must be a size 2 tuple but {shape} was given"

    if rng is None:
        rng = np.random.default_rng()

    nrows, ncols = shape

    nnz = int(nrows * ncols * density_ratio)

    data = np.ones(nnz, dtype=dtype)
    rows = rng.integers(0, nrows, nnz)
    cols = rng.integers(0, ncols, nnz)

    coo = coo_matrix((data, (rows, cols)), shape=shape)

    # get rid of dup rows/cols
    coo.sum_duplicates()

    coo.data[:] = 1  # set all nonzero values to 1. For bool won't matter, but need for other dtypes

    return coo


def sparse_subinds(orig, inds):
    """
    Compute new rows or cols resulting from applying inds on top of an existing sparsity pattern.

    This only comes into play when we have an approx total jacobian where some dv/resp have
    indices.

    Parameters
    ----------
    orig : ndarray
        Either row or col indices (part of a subjac sparsity pattern).
    inds : ndarray or list
        Sub-indices introduced when adding a desvar or response.

    Returns
    -------
    ndarray
        New compressed rows or cols.
    ndarray
        Mask array that can be used to update subjac value and corresponding index array to orig.
    """
    mask = np.zeros(orig.size, dtype=bool)
    for i in inds:
        mask |= orig == i
    newsp = orig[mask]

    # replace the index with the 'compressed' index after we've masked out entries
    for r, i in enumerate(np.sort(inds)):
        newsp[newsp == i] = r

    return newsp, mask


def identity_column_iter(column):
    """
    Yield the given column with a 1 in each position.

    This is useful if you don't want to allocate memory for the full sized identity matrix.
    Note that this reuses the column array and assumes that the column array has not
    been modified outside of this function.

    Parameters
    ----------
    column : ndarray
        The array that will contain a column of the 'virtual' identity matrix.

    Yields
    ------
    ndarray
        A column of the identity matrix.
    """
    column[:] = 0
    for i in range(column.size):
        column[i - 1] = 0
        column[i] = 1
        yield column


def array_hash(arr, alg=hashlib.sha1):
    """
    Return a hash of the given numpy array.

    arr must be C-contiguous.

    Parameters
    ----------
    arr : ndarray
        The array to be hashed.
    alg : hashing algorithm
        Algorithm defaults to hashlib.sha1.

    Returns
    -------
    str
        The computed hash.
    """
    return alg(arr.view(np.uint8)).hexdigest()


_randgen = np.random.default_rng()


def get_random_arr(shape, comm=None, generator=None):
    """
    Request a random array, ensuring that its value will be consistent across MPI processes.

    Parameters
    ----------
    shape : int
        Shape of the random array.
    comm : MPI communicator or None
        All members of this communicator will receive the random array.
    generator : random number generator or None
        If not None, use this as the random number generator if on rank 0.

    Returns
    -------
    ndarray
        The random array.
    """
    gen = generator if generator is not None else _randgen
    if comm is None or comm.size == 1:
        return gen.random(shape)

    if comm.rank == 0:
        arr = gen.random(shape)
    else:
        arr = np.empty(shape)
    comm.Bcast(arr, root=0)
    return arr


class ValueRepeater(object):
    """
    An iterable over a single value that repeats a given number of times.

    Parameters
    ----------
    val : object
        The value to be repeated.
    size : int
        The number of times to repeat the value.

    Attributes
    ----------
    val : object
        The value to be repeated.
    size : int
        The number of times to repeat the value.

    Yields
    ------
    object
        The value.
    """

    def __init__(self, val, size):
        """
        Initialize all attributes.
        """
        self.val = val
        self.size = size

    def __iter__(self):
        """
        Return an iterator over the value.

        Yields
        ------
        object
            The value.
        """
        for i in range(self.size):
            yield self.val

    def __len__(self):
        """
        Return the size of the value.

        Returns
        -------
        int
            The size of the value.
        """
        return self.size

    def __contains__(self, item):
        """
        Return True if the given item is equal to the value.

        Parameters
        ----------
        item : object
            The item to be checked for containment.
        """
        return item == self.val

    def __getitem__(self, idx):
        """
        Return the value.

        Parameters
        ----------
        idx : int
            The index of the value to be returned.
        """
        i = idx
        if idx < 0:
            idx += self.size
        if idx >= self.size:
            raise IndexError(f"index {i} is out of bounds for size {self.size}")
        return self.val


def convert_nans_in_nested_list(val_as_list):
    """
    Given a list, possibly nested, replace any numpy.nan values with the string "nan".

    This is done since JSON does not handle nan. This code is used to pass variable values
    to the N2 diagram.

    The modifications to the list values are done in-place to avoid excessive copying of lists.

    Parameters
    ----------
    val_as_list : list
        List, possibly nested, whose nan elements need to be converted.
    """
    for i, val in enumerate(val_as_list):
        if isinstance(val, list):
            convert_nans_in_nested_list(val)
        else:
            if np.isnan(val):
                val_as_list[i] = "nan"
            elif np.isinf(val):
                val_as_list[i] = "infinity"
            else:
                val_as_list[i] = val


def convert_ndarray_to_support_nans_in_json(val):
    """
    Given numpy array of arbitrary dimensions, return the equivalent nested list with nan replaced.

    numpy.nan values are replaced with the string "nan".

    Parameters
    ----------
    val : ndarray
        Numpy array to be converted.

    Returns
    -------
    list
        The equivalent list (possibly nested) with any nan values replaced with the string "nan".
    """
    val = np.asarray(val)

    # do a quick check for any nans or infs and if not we can avoid the slow check
    if not (np.any(np.isnan(val)) or np.any(np.isinf(val))):
        return val.tolist()

    val_as_list = val.tolist()
    convert_nans_in_nested_list(val_as_list)
    return val_as_list


if numba is None:
    allclose = np.allclose

    def allzero(a):
        """
        Return True if all elements of a are zero.

        Parameters
        ----------
        a : ndarray
            Array to be checked for zeros.

        Returns
        -------
        bool
            True if all elements of a are zero.
        """
        return not np.any(a)

else:

    @numba.jit(nopython=True, nogil=True)
    def allclose(a, b, rtol=3e-16, atol=3e-16):
        """
        Return True if all elements of a and b pass a tolerance check.

        The tolerance check is:
            abs(a - b) <= atol + rtol * abs(b)

        Returns when the first non-close element is found.

        Parameters
        ----------
        a : ndarray
            First array to be compared.
        b : ndarray
            Second array to be compared.
        rtol : float
            Relative tolerance for comparison.
        atol : float
            Absolute tolerance for comparison.

        Returns
        -------
        bool
            True if all elements of a and b are close within the given absolute and
            relative tolerance.
        """
        if a.size != b.size:
            return False

        for aval, bval in zip(a, b):
            abs_err = aval - bval

            if abs_err < 0.:
                abs_err = -abs_err

            if abs_err > atol:
                return False

            if bval < 0.:
                bval = -bval

            if abs_err > atol + rtol * bval:
                return False

        return True

    @numba.jit(nopython=True, nogil=True)
    def allzero(a):
        """
        Return True if all elements of a are zero.

        Unlike np.any, this returns as soon as a non-zero element is found and so can be
        faster for arrays having nonzero values.  It's comparable in speed (slighly faster) to
        'not np.any' for arrays that are all zeros.

        Parameters
        ----------
        a : ndarray
            Array to be checked for zeros.

        Returns
        -------
        bool
            True if all elements of a are zero.
        """
        for i in range(len(a)):
            if a[i] != 0.:
                return False
        return True


def submat_sparsity_iter(row_var_size_iter, col_var_size_iter, nzrows, nzcols, shape):
    """
    Yield the sparsity of each submatrix, based on variable names and sizes.

    Parameters
    ----------
    row_var_size_iter : iterator of (name, size)
        Iterator of row variable names and sizes.
    col_var_size_iter : iterator of (name, size)
        Iterator of column variable names and sizes.
    nzrows : ndarray
        Row indices of nonzero entries in the full matrix.
    nzcols : ndarray
        Column indices of nonzero entries in the full matrix.
    shape : tuple
        Shape of the full matrix.

    Yields
    ------
    tuple
        (row_varname, col_varname, nonzero rows, nonzero cols, shape)
    """
    row_start = row_end = 0

    data = np.ones(nzrows.size, dtype=np.int8)
    csr = csr_matrix((data, (nzrows, nzcols)), shape=shape)
    col_iter = list(col_var_size_iter)  # need to iterate over multiple times

    for of, of_size in row_var_size_iter:
        row_end += of_size
        rowslice = csr[row_start:row_end, :]
        row_start = row_end

        csc = rowslice.tocsc()
        col_start = col_end = 0
        for wrt, wrt_size in col_iter:
            col_end += wrt_size
            submat = csc[:, col_start:col_end].tocoo()
            col_start = col_end

            yield (of, wrt, submat.row, submat.col, submat.shape)


def safe_norm(arr):
    """
    Return the norm of the given array, or 0 if the array is None or empty.

    Parameters
    ----------
    arr : ndarray or None
        Array to be normed.

    Returns
    -------
    float
        Norm of the array or 0 if the array is None or empty.
    """
    return 0. if arr is None or arr.size == 0 else np.linalg.norm(arr)


def get_tol_violation(x, ref, atol=0.0, rtol=1e-5):
    """
    Compute the max tolerance violation of the difference between x and ref.

    tolerance violation is defined as:

        abs(x - ref) - (atol + rtol * abs(ref))

    If the result is positive, then the tolerance is violated.

    Parameters
    ----------
    x : ndarray
        The test array.
    ref : ndarray
        The reference array.
    atol : float
        The absolute tolerance.
    rtol : float
        The relative tolerance.

    Returns
    -------
    tuple
        Max_error, (max_x_location, max_ref_location), above_tolerance.
    """
    abs_error = np.abs(x - ref)
    if abs_error.size == 0:
        return 0.0, (0, 0), False, 0.0, 0.0

    mixed_atol_rtol = atol + rtol * np.abs(ref)
    diff = abs_error - mixed_atol_rtol  # any values > 0 violate tolerance check

    max_error_idx = np.argmax(diff)
    max_error = diff.flat[max_error_idx]
    max_error_x = x.flat[max_error_idx]
    max_error_ref = ref.flat[max_error_idx]
    abs_at_max = abs_error.flat[max_error_idx]
    if max_error_ref:
        rel_at_max = abs_at_max / np.abs(max_error_ref)
    else:
        rel_at_max = np.inf

    return max_error, (max_error_x, max_error_ref), np.any(diff > 0.), abs_at_max, rel_at_max


_unsigned = (np.uint8, np.uint16, np.uint32, np.uint64)
_signed = (np.int8, np.int16, np.int32, np.int64)


def get_index_dtype(size, allow_negative=False):
    """
    Return the dtype of the index array for the given size.

    Parameters
    ----------
    size : int
        The size of the array.
    allow_negative : bool
        If True, allow negative indices.

    Returns
    -------
    dtype
        The dtype of the index array.
    """
    dtypes = _signed if allow_negative else _unsigned
    for dtype in dtypes:
        if size <= np.iinfo(dtype).max + 1:
            return dtype
    raise ValueError(f"size {size} is too large to be represented as an index")
