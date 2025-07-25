"""LinearSolver that uses linalg.solve or LU factor/solve."""

import warnings

import numpy as np
import scipy.linalg
import scipy.sparse.linalg
from scipy.sparse import csc_matrix

from openmdao.solvers.solver import LinearSolver
from openmdao.matrices.dense_matrix import DenseMatrix
from openmdao.utils.array_utils import identity_column_iter
from openmdao.solvers.linear.linear_rhs_checker import LinearRHSChecker


def index_to_varname(system, loc):
    """
    Given a matrix location, return the name of the variable associated with that index.

    Parameters
    ----------
    system : <System>
        System containing the Directsolver.
    loc : int
        Index of row or column.

    Returns
    -------
    str
        String containing variable absolute name (and promoted name if there is one) and index.
    """
    start = end = 0
    varsizes = np.sum(system._owned_output_sizes, axis=0)
    for i, name in enumerate(system._resolver.abs_iter('output')):
        end += varsizes[i]
        if loc < end:
            varname = system._resolver.abs2prom(name, 'output')
            break
        start = end

    if varname == name:
        name_string = "'{}' index {}.".format(varname, loc - start)
    else:
        name_string = "'{}' ('{}') index {}.".format(varname, name, loc - start)

    return name_string


def loc_to_error_msg(system, loc_txt, loc):
    """
    Given a matrix location, format a coherent error message when matrix is singular.

    Parameters
    ----------
    system : <System>
        System containing the Directsolver.
    loc_txt : str
        Either 'row' or 'col'.
    loc : int
        Index of row or column.

    Returns
    -------
    str
        New error string.
    """
    names = index_to_varname(system, loc)
    msg = "Singular entry found in {} for {} associated with state/residual " + names
    return msg.format(system.msginfo, loc_txt)


def format_singular_error(system, matrix):
    """
    Format a coherent error message for any ill-conditioned mmatrix.

    Parameters
    ----------
    system : <System>
        System containing the Directsolver.
    matrix : ndarray
        Matrix of interest.

    Returns
    -------
    str
        New error string.
    """
    if scipy.sparse.issparse(matrix):
        matrix = matrix.toarray()

    if np.any(np.isnan(matrix)):
        # There is a nan in the matrix.
        return format_nan_error(system, matrix)

    zero_rows = np.where(~matrix.any(axis=1))[0]
    zero_cols = np.where(~matrix.any(axis=0))[0]
    if zero_cols.size <= zero_rows.size:

        if zero_rows.size == 0:
            # In this case, some row is a linear combination of the other rows.

            # SVD gives us some information that may help locate the source of the problem.
            try:
                u, _, _ = np.linalg.svd(matrix)

            except Exception:
                msg = f"Jacobian in '{system.pathname}' is not full rank, but OpenMDAO was " + \
                      "not able to determine which rows or columns."
                return msg

            # Nonzero elements in the left singular vector show the rows that contribute strongly to
            # the singular subspace. Note that sometimes extra rows/cols are included in the set,
            # currently don't have a good way to pare them down.
            tol = 1e-15
            u_sing = np.abs(u[:, -1])
            left_idx = np.where(u_sing > tol)[0]

            msg = "Jacobian in '{}' is not full rank. The following set of states/residuals " + \
                  "contains one or more equations that is a linear combination of the others: \n"

            for loc in left_idx:
                name = index_to_varname(system, loc)
                msg += ' ' + name + '\n'

            if len(left_idx) > 2:
                msg += "Note that the problem may be in a single Component."

            return msg.format(system.pathname)

        loc_txt = "row"
        loc = zero_rows[0]
    else:
        loc_txt = "column"
        loc = zero_cols[0]

    return loc_to_error_msg(system, loc_txt, loc)


def format_nan_error(system, matrix):
    """
    Format a coherent error message when the matrix contains NaN.

    Parameters
    ----------
    system : <System>
        System containing the Directsolver.
    matrix : ndarray
        Matrix of interest.

    Returns
    -------
    str
        New error string.
    """
    # Because of how we built the matrix, a NaN in a comp causes the whole row to be NaN, so we
    # need to associate each index with a variable.
    varsizes = np.sum(system._owned_output_sizes, axis=0)

    nanrows = np.zeros(matrix.shape[0], dtype=bool)
    nanrows[np.where(np.isnan(matrix))[0]] = True

    varnames = []
    start = end = 0
    for i, name in enumerate(system._resolver.abs_iter('output')):
        end += varsizes[i]
        if np.any(nanrows[start:end]):
            varnames.append("'%s'" % system._resolver.abs2prom(name, 'output'))
        start = end

    msg = "NaN entries found in {} for rows associated with states/residuals [{}]."
    return msg.format(system.msginfo, ', '.join(varnames))


class DirectSolver(LinearSolver):
    """
    LinearSolver that uses linalg.solve or LU factor/solve.

    Parameters
    ----------
    **kwargs : dict
        Options dictionary.

    Attributes
    ----------
    _lin_rhs_checker : LinearRHSChecker or None
        Object for checking the right-hand side of the linear solve.
    """

    SOLVER = 'LN: Direct'

    def __init__(self, **kwargs):
        """
        Declare the solver options.
        """
        super().__init__(**kwargs)
        self._lin_rhs_checker = None

    def _declare_options(self):
        """
        Declare options before kwargs are processed in the init method.
        """
        super()._declare_options()

        self.options.declare('err_on_singular', types=bool, default=True,
                             desc="Raise an error if LU decomposition is singular.")

        self.options.declare('rhs_checking', types=(bool, dict),
                             default=False,
                             desc="If True, check RHS vs. cache and/or zero to avoid some solves."
                             "Can also be set to a dict of options for the LinearRHSChecker to "
                             "allow finer control over it. Allowed options are: "
                             f"{LinearRHSChecker.options}")

        # this solver does not iterate
        self.options.undeclare("maxiter")
        self.options.undeclare("err_on_non_converge")

        self.options.undeclare("atol")
        self.options.undeclare("rtol")

        # Use an assembled jacobian by default.
        self.options['assemble_jac'] = True

        self.supports['implicit_components'] = True

    def _setup_solvers(self, system, depth):
        """
        Assign system instance, set depth, and optionally perform setup.

        Parameters
        ----------
        system : <System>
            pointer to the owning system.
        depth : int
            depth of the current system (already incremented).
        """
        super()._setup_solvers(system, depth)
        self._disallow_distrib_solve()
        self._lin_rhs_checker = LinearRHSChecker.create(self._system(),
                                                        self.options['rhs_checking'])

    def _linearize_children(self):
        """
        Return a flag that is True when we need to call linearize on our subsystems' solvers.

        Returns
        -------
        boolean
            Flag for indicating child linearization.
        """
        return False

    def can_solve_cycle(self):
        """
        Return True if this solver can solve groups with cycles.

        Returns
        -------
        bool
            True if this solver can solve groups with cycles.
        """
        return True

    def use_relevance(self):
        """
        Return True if relevance should be active.

        Returns
        -------
        bool
            True if relevance should be active.
        """
        return False

    def _build_mtx(self):
        """
        Assemble a Jacobian matrix by matrix-vector-product with columns of identity.

        Returns
        -------
        ndarray
            Jacobian matrix.
        """
        system = self._system()
        bvec = system._dresiduals
        xvec = system._doutputs

        # First make a backup of the vectors
        b_data = bvec.asarray(copy=True)
        x_data = xvec.asarray(copy=True)

        nmtx = x_data.size
        seed = np.zeros(x_data.size)
        mtx = np.empty((nmtx, nmtx), dtype=b_data.dtype)
        scope_out, scope_in = system._get_matvec_scope()

        # temporarily disable relevance to avoid creating a singular matrix
        with system._relevance.active(False):
            # Assemble the Jacobian by running the identity matrix through apply_linear
            for i, seed in enumerate(identity_column_iter(seed)):
                # set value of x vector to provided value
                xvec.set_val(seed)

                # apply linear
                system._apply_linear('fwd', scope_out, scope_in)

                # put new value in out_vec
                mtx[:, i] = bvec.asarray()

        # Restore the backed-up vectors
        bvec.set_val(b_data)
        xvec.set_val(x_data)

        return mtx

    def _linearize(self):
        """
        Perform factorization.
        """
        system = self._system()
        nproc = system.comm.size

        if system._get_assembled_jac() is not None:
            matrix = system._assembled_jac.get_dr_do_matrix()

            if matrix is None:
                # this happens if we're not rank 0 when using owned_sizes
                self._lu = self._lup = None

            # Perform dense or sparse lu factorization.
            elif isinstance(matrix, csc_matrix):
                try:
                    self._lu = scipy.sparse.linalg.splu(matrix)
                except RuntimeError:
                    raise RuntimeError(format_singular_error(system, matrix))

            elif isinstance(matrix, np.ndarray):  # dense
                # During LU decomposition, detect singularities and warn user.
                with warnings.catch_warnings():
                    if self.options['err_on_singular']:
                        warnings.simplefilter('error', RuntimeWarning)
                    try:
                        self._lup = scipy.linalg.lu_factor(matrix)
                    except RuntimeWarning:
                        raise RuntimeError(format_singular_error(system, matrix))

                    # NaN in matrix.
                    except ValueError:
                        raise RuntimeError(format_nan_error(system, matrix))

            # Note: calling scipy.sparse.linalg.splu on a COO actually transposes
            # the matrix during conversion to csc prior to LU decomp, so we can't use COO.
            else:
                raise RuntimeError("Direct solver not implemented for matrix type %s"
                                   " in %s." % (type(matrix), system.msginfo))
        else:
            if nproc > 1:
                raise RuntimeError("DirectSolvers without an assembled jacobian are not supported "
                                   "when running under MPI if comm.size > 1.")

            mtx = self._build_mtx()

            # During LU decomposition, detect singularities and warn user.
            with warnings.catch_warnings():

                if self.options['err_on_singular']:
                    warnings.simplefilter('error', RuntimeWarning)

                try:
                    self._lup = scipy.linalg.lu_factor(mtx)

                except RuntimeWarning:
                    raise RuntimeError(format_singular_error(system, mtx))

                # NaN in matrix.
                except ValueError:
                    raise RuntimeError(format_nan_error(system, mtx))

        if self._lin_rhs_checker is not None:
            self._lin_rhs_checker.clear()

    def _inverse(self):
        """
        Return the inverse Jacobian.

        This is only used by the Broyden solver when calculating a full model Jacobian. Since it
        is only done for a single RHS, no need for LU.

        Returns
        -------
        ndarray
            Inverse Jacobian.
        """
        system = self._system()
        nproc = system.comm.size

        if system._get_assembled_jac() is not None:
            matrix = system._assembled_jac.get_dr_do_matrix()

            if matrix is None:
                # This happens if we're not rank 0 and owned_sizes are being used
                sz = np.sum(system._owned_output_sizes)
                inv_jac = np.zeros((sz, sz))

            # Dense and Sparse matrices have their own inverse method.
            elif isinstance(matrix, np.ndarray):
                # Detect singularities and warn user.
                with warnings.catch_warnings():
                    if self.options['err_on_singular']:
                        warnings.simplefilter('error', RuntimeWarning)
                    try:
                        inv_jac = scipy.linalg.inv(matrix)
                    except RuntimeWarning:
                        raise RuntimeError(format_singular_error(system, matrix))

                    # NaN in matrix.
                    except ValueError:
                        raise RuntimeError(format_nan_error(system, matrix))

            elif isinstance(matrix, csc_matrix):
                try:
                    inv_jac = scipy.sparse.linalg.inv(matrix)
                except RuntimeError:
                    raise RuntimeError(format_singular_error(system, matrix))

                # to prevent broadcasting errors later, make sure inv_jac is 2D
                # scipy.sparse.linalg.inv returns a shape (1,) array if matrix is shape (1,1)
                if inv_jac.size == 1:
                    inv_jac = inv_jac.reshape((1, 1))
            else:
                raise RuntimeError("Direct solver not implemented for matrix type %s"
                                   " in %s." % (type(matrix), system.msginfo))

        else:
            if nproc > 1:
                raise RuntimeError("BroydenSolvers without an assembled jacobian are not supported "
                                   "when running under MPI if comm.size > 1.")
            mtx = self._build_mtx()

            # During inversion detect singularities and warn user.
            with warnings.catch_warnings():

                if self.options['err_on_singular']:
                    warnings.simplefilter('error', RuntimeWarning)

                try:
                    inv_jac = scipy.linalg.inv(mtx)

                except RuntimeWarning:
                    raise RuntimeError(format_singular_error(system, mtx))

                # NaN in matrix.
                except ValueError:
                    raise RuntimeError(format_nan_error(system, mtx))

        return inv_jac

    def solve(self, mode, rel_systems=None):
        """
        Run the solver.

        Parameters
        ----------
        mode : str
            'fwd' or 'rev'.
        rel_systems : set of str
            Names of systems relevant to the current solve.  Deprecated.
        """
        system = self._system()

        d_residuals = system._dresiduals
        d_outputs = system._doutputs

        # assign x and b vectors based on mode
        if mode == 'fwd':
            x_vec = d_outputs.asarray()
            b_vec = d_residuals.asarray()
            trans_lu = 0
            trans_splu = 'N'
        else:  # rev
            x_vec = d_residuals.asarray()
            b_vec = d_outputs.asarray()
            trans_lu = 1
            trans_splu = 'T'

            if self._lin_rhs_checker is not None:
                sol_array, is_zero = self._lin_rhs_checker.get_solution(b_vec, system)
                if is_zero:
                    x_vec[:] = 0.0
                    return
                if sol_array is not None:
                    x_vec[:] = sol_array
                    return

        # AssembledJacobians are unscaled.
        if system._get_assembled_jac() is not None:
            full_b = b_vec

            with system._unscaled_context(outputs=[d_outputs], residuals=[d_residuals]):
                if isinstance(system._assembled_jac._dr_do_mtx, DenseMatrix):
                    sol_array = scipy.linalg.lu_solve(self._lup, full_b, trans=trans_lu)
                else:
                    sol_array = self._lu.solve(full_b, trans_splu)

                x_vec[:] = sol_array

        # matrix-vector-product generated jacobians are scaled.
        else:
            x_vec[:] = sol_array = scipy.linalg.lu_solve(self._lup, b_vec, trans=trans_lu)

        if not system.under_complex_step and self._lin_rhs_checker is not None and mode == 'rev':
            self._lin_rhs_checker.add_solution(b_vec, sol_array, copy=True)

    def preferred_sparse_format(self):
        """
        Return the preferred sparse format for the dr/do matrix of a split jacobian.

        Returns
        -------
        str
            The preferred sparse format for the dr/do matrix of a split jacobian.
        """
        return 'csc'
