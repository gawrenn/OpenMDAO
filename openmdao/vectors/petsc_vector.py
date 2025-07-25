"""Define the PETSc Vector class."""
from openmdao.utils.mpi import MPI

CITATION = '''@InProceedings{petsc-efficient,
    Author = "Satish Balay and William D. Gropp and Lois Curfman McInnes and Barry F. Smith",
    Title = "Efficient Management of Parallelism in Object Oriented Numerical Software Libraries",
    Booktitle = "Modern Software Tools in Scientific Computing",
    Editor = "E. Arge and A. M. Bruaset and H. P. Langtangen",
    Pages = "163--202",
    Publisher = "Birkh{\"{a}}user Press",
    Year = "1997"
}'''

if MPI is None:
    PETScVector = None
else:
    import numpy as np

    from petsc4py import PETSc
    from openmdao.core.constants import INT_DTYPE
    from openmdao.vectors.default_vector import DefaultVector
    from openmdao.vectors.petsc_transfer import PETScTransfer

    class PETScVector(DefaultVector):
        """
        PETSc Vector implementation for running in parallel.

        Most methods use the DefaultVector's implementation.

        Parameters
        ----------
        name : str
            The name of the vector: 'nonlinear' or 'linear'.
        kind : str
            The kind of vector, 'input', 'output', or 'residual'.
        system : <System>
            The owning system.
        parent_vector : <Vector>
            Parent vector.
        alloc_complex : bool
            Whether to allocate any imaginary storage to perform complex step. Default is False.

        Attributes
        ----------
        _dup_inds : ndarray of int
            Array of indices of variables that aren't locally owned, meaning that they duplicate
            variables that are 'owned' by a different process. Used by certain distributed
            calculations, e.g., get_norm(), where including duplicate values would result in
            the wrong answer.
        _dup_scratch : ndarray of float or None
            If the array has dups, this scratch array will be created to store the de-duped
            version.
        _comm : MPI.Comm
            The MPI communicator for the owning system.
        """

        TRANSFER = PETScTransfer
        cite = CITATION
        distributed = True

        def __init__(self, name, kind, system, parent_vector=None, alloc_complex=False):
            """
            Initialize all attributes.
            """
            self._dup_inds = None
            self._dup_scratch = None
            self._comm = system.comm
            self._petsc = None
            self._imag_petsc = None

            super().__init__(name, kind, system, parent_vector, alloc_complex)

        def _initialize_data(self, parent_vector, system):
            """
            Internally allocate vectors.

            Parameters
            ----------
            parent_vector : <Vector>
                Parent vector.
            system : <System>
                The owning system.
            """
            super()._initialize_data(parent_vector, system)

            data = self._data.real

            if self._alloc_complex:
                self._petsc = PETSc.Vec().createWithArray(data.copy(), comm=system.comm)
            else:
                self._petsc = PETSc.Vec().createWithArray(data, comm=system.comm)

            # Allocate imaginary for complex step
            if self._alloc_complex:
                data = self._data.imag
                self._imag_petsc = PETSc.Vec().createWithArray(data, comm=system.comm)

            self._init_dup_inds(system)

        def _init_dup_inds(self, system):
            """
            Compute the indices into the data vector corresponding to non-distributed variables.

            Returns
            -------
            ndarray of int
                Index array corresponding to non-distributed variables.
            """
            if system.comm.size > 1:
                # Here, we find the indices that are not locally owned so that we can
                # temporarilly zero them out for the norm calculation.
                dup_inds = []
                abs2meta = system._var_allprocs_abs2meta[self._iotype]
                for name, start, stop in self.ranges():
                    owning_rank = system._owning_rank[name]
                    if not abs2meta[name]['distributed'] and owning_rank != system.comm.rank:
                        dup_inds.extend(range(start, stop))

                self._dup_inds = np.array(dup_inds, dtype=INT_DTYPE)
                if len(dup_inds) > 0:
                    self._dup_scratch = np.empty(stop)
            else:
                self._dup_inds = np.array([], dtype=INT_DTYPE)

            return self._dup_inds

        def _get_nodup(self):
            """
            Retrieve a version of the data vector with any duplicate variables zeroed out.

            Returns
            -------
            ndarray
                Array the same size as our data array with duplicate variables zeroed out.
                If all variables are owned by this process, then the data array itself is
                returned without copying.
            """
            if self._dup_inds.size > 0:
                self._dup_scratch[:] = self.asarray()
                self._dup_scratch[self._dup_inds] = 0.0
                return self._dup_scratch

            return self._get_data()

        def get_norm(self):
            """
            Return the 2 norm of this vector.

            Returns
            -------
            float
                Norm of this vector.
            """
            return self._comm.allreduce(np.sum(self._get_nodup() ** 2)) ** 0.5

        def dot(self, vec):
            """
            Compute the dot product of the real parts of the current vec and the incoming vec.

            Parameters
            ----------
            vec : <Vector>
                The incoming vector being dotted with self.

            Returns
            -------
            float
                The computed dot product value.
            """
            return self._comm.allreduce(np.dot(self._get_nodup(), vec._get_data()))
