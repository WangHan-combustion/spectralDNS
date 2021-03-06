__author__ = "Mikael Mortensen <mikaem@math.uio.no>"
__date__ = "2015-10-29"
__copyright__ = "Copyright (C) 2015-2018 " + __author__
__license__ = "GNU Lesser GPL version 3 or any later version"

#pylint: disable=unused-variable,function-redefined,unused-argument

from .KMM_mpifft4py import *
from .spectralinit import end_of_tstep

def get_context():
    # Get points and weights for Chebyshev weighted integrals
    ST = ShenDirichletBasis(params.N[0], quad=params.Dquad)
    SB = ShenBiharmonicBasis(params.N[0], quad=params.Bquad)
    CT = ST.CT

    ST_pad = ShenDirichletBasis(params.N[0], quad=params.Dquad)
    SB_pad = ShenBiharmonicBasis(params.N[0], quad=params.Bquad)
    CT_pad = ST.CT  # Chebyshev transform

    Nu = params.N[0]-2   # Number of velocity modes in Shen basis
    Nb = params.N[0]-4   # Number of velocity modes in Shen biharmonic basis
    u_slice = slice(0, Nu)
    v_slice = slice(0, Nb)

    FST = SlabShen_R2C(params.N, params.L, comm, threads=params.threads,
                       communication=params.communication,
                       planner_effort=params.planner_effort,
                       dealias_cheb=params.dealias_cheb)

    float, complex, mpitype = datatypes("double")

    ST.plan(FST.complex_shape(), 0, complex, {'threads':params.threads,
                                              'planner_effort':params.planner_effort["dct"]})
    SB.plan(FST.complex_shape(), 0, complex, {'threads':params.threads,
                                              'planner_effort':params.planner_effort["dct"]})

    # Mesh variables
    X = FST.get_local_mesh(ST)
    x0, x1, x2 = FST.get_mesh_dims(ST)
    K = FST.get_local_wavenumbermesh(scaled=True)

    K2 = K[1]*K[1]+K[2]*K[2]
    K4 = K2**2

    # Set Nyquist frequency to zero on K that is used for odd derivatives in nonlinear terms
    Kx = FST.get_local_wavenumbermesh(scaled=True, eliminate_highest_freq=True)
    K_over_K2 = zeros((2,) + FST.complex_shape())
    for i in range(2):
        K_over_K2[i] = K[i+1] / np.where(K2 == 0, 1, K2)

    # Solution variables
    U = zeros((3,)+FST.real_shape(), dtype=float)
    U_hat = zeros((3,)+FST.complex_shape(), dtype=complex)
    g = zeros(FST.complex_shape(), dtype=complex)

    # primary variable
    u = (U_hat, g)

    nu, dt, N = params.nu, params.dt, params.N

    H_hat = zeros((3,)+FST.complex_shape(), dtype=complex)

    dU = zeros((3,)+FST.complex_shape(), dtype=complex)
    hv = zeros((2,)+FST.complex_shape(), dtype=complex)
    hg = zeros((2,)+FST.complex_shape(), dtype=complex)
    h1 = zeros((2, 2, N[0]), dtype=complex)

    Source = zeros((3,)+FST.real_shape(), dtype=float)
    Sk = zeros((3,)+FST.complex_shape(), dtype=complex)

    work = work_arrays()

    # RK parameters
    a = (8./15., 5./12., 3./4.)
    b = (0.0, -17./60., -5./12.)

    # Collect all linear algebra solvers
    # RK 3 requires three solvers because of the three different coefficients
    rk = 0
    la = config.AttributeDict(
        dict(HelmholtzSolverG=[Helmholtz(N[0], np.sqrt(K2[0]+2.0/nu/(a[rk]+b[rk])/dt), ST)
                               for rk in range(3)],
             BiharmonicSolverU=[Biharmonic(N[0], -nu*(a[rk]+b[rk])*dt/2., 1.+nu*(a[rk]+b[rk])*dt*K2[0],
                                           -(K2[0] + nu*(a[rk]+b[rk])*dt/2.*K4[0]), quad=SB.quad,
                                           solver="cython") for rk in range(3)],
             HelmholtzSolverU0=[Helmholtz(N[0], np.sqrt(2./nu/(a[rk]+b[rk])/dt), ST) for rk in range(3)],
             TDMASolverD=TDMA(inner_product((ST, 0), (ST, 0)))))

    alfa = K2 - 2.0/nu/dt
    # Collect all matrices
    mat = config.AttributeDict(
        dict(CDD=inner_product((ST, 0), (ST, 1)),
             AC=[BiharmonicCoeff(N[0], nu*(a[rk]+b[rk])*dt/2., (1. - nu*(a[rk]+b[rk])*dt*K2),
                                 -(K2 - nu*(a[rk]+b[rk])*dt/2.*K4), SB.quad) for rk in range(3)],
             AB=[HelmholtzCoeff(N[0], 1.0, -(K2 - 2.0/nu/dt/(a[rk]+b[rk])), ST.quad) for rk in range(3)],
             # Matrices for biharmonic equation
             CBD=inner_product((SB, 0), (ST, 1)),
             ABB=inner_product((SB, 0), (SB, 2)),
             BBB=inner_product((SB, 0), (SB, 0)),
             SBB=inner_product((SB, 0), (SB, 4)),
             # Matrices for Helmholtz equation
             ADD=inner_product((ST, 0), (ST, 2)),
             BDD=inner_product((ST, 0), (ST, 0)),
             BBD=inner_product((SB, 0), (ST, 0)),
             CDB=inner_product((ST, 0), (SB, 1))))
    del rk

    hdf5file = KMMRK3Writer({"U":U[0], "V":U[1], "W":U[2]},
                            chkpoint={'current':{'U':U}, 'previous':{}},
                            filename=params.solver+".h5",
                            mesh={"x": x0, "y": x1, "z": x2})

    return config.AttributeDict(locals())

class KMMRK3Writer(HDF5Writer):
    def update_components(self, **context):
        """Transform to real data when storing the solution"""
        U = get_velocity(**context)    # updates U from U_hat

@optimizer
def add_linear(rhs, u, g, work, AB, AC, SBB, ABB, BBB, nu, dt, K2, K4, a, b):
    diff_u = work[(g, 0)]
    diff_g = work[(g, 1, False)]
    w0 = work[(g, 2, False)]

    # Compute diffusion for g-equation
    diff_g = AB.matvec(g, diff_g)

    # Compute diffusion++ for u-equation
    diff_u[:] = nu*(a+b)*dt/2.*SBB.matvec(u, w0)
    diff_u += (1. - nu*(a+b)*dt*K2)*ABB.matvec(u, w0)
    diff_u -= (K2 - nu*(a+b)*dt/2.*K4)*BBB.matvec(u, w0)

    rhs[0] += diff_u
    rhs[1] += diff_g

    return rhs

def ComputeRHS(rhs, u_hat, g_hat, rk, solver,
               H_hat, FST, ST, SB, work, Kx, K2, K4, hv,
               hg, a, b, la, mat, **context):

    """Compute right hand side of Navier Stokes

    args:
        rhs         The right hand side to be returned
        u_hat       The FST of the velocity at current time.
        g_hat       The FST of the curl in wall normal direction
        rk          The step in the Runge Kutta integrator
        solver      The current solver module

    Remaining args are extracted from context

    """

    # Nonlinear convection term at current u_hat
    H_hat = solver.conv(H_hat, u_hat, g_hat, Kx, FST, SB, ST, work, mat, la)

    w0 = work[(H_hat[0], 0, False)]
    w1 = work[(H_hat[0], 1, False)]
    hv[1] = -K2*mat.BBD.matvec(H_hat[0], w0)
    #hv[:] = FST.scalar_product(H[0], hv, SB)
    #hv *= -K2
    hv[1] -= 1j*Kx[1]*mat.CBD.matvec(H_hat[1], w0)
    hv[1] -= 1j*Kx[2]*mat.CBD.matvec(H_hat[2], w0)
    hg[1] = 1j*Kx[1]*mat.BDD.matvec(H_hat[2], w0) - 1j*Kx[2]*mat.BDD.matvec(H_hat[1], w1)

    rhs[0] = (hv[1]*a[rk] + hv[0]*b[rk])*params.dt
    rhs[1] = (hg[1]*a[rk] + hg[0]*b[rk])*2./params.nu/(a[rk]+b[rk])

    hv[0] = hv[1]
    hg[0] = hg[1]

    rhs = solver.add_linear(rhs, u_hat[0], g_hat, work, mat.AB[rk], mat.AC[rk],
                            mat.SBB, mat.ABB, mat.BBB, params.nu, params.dt,
                            K2, K4, a[rk], b[rk])
    return rhs

def solve_linear(u_hat, g_hat, rhs, rk,
                 work, la, mat, H_hat, Sk, h1, a, b, K_over_K2, **context):

    f_hat = work[(u_hat[0], 0)]
    w0 = work[(u_hat[0], 1, False)]

    u_hat[0] = la.BiharmonicSolverU[rk](u_hat[0], rhs[0])
    g_hat = la.HelmholtzSolverG[rk](g_hat, rhs[1])

    if rank == 0:
        u0_hat = work[((2, params.N[0]), complex, 0)]
        h0_hat = work[((2, params.N[0]), complex, 1)]
        u0_hat[0] = u_hat[1, :, 0, 0]
        u0_hat[1] = u_hat[2, :, 0, 0]

    # Compute v_hat and w_hat from u_hat and g_hat
    f_hat -= mat.CDB.matvec(u_hat[0], w0)
    f_hat = la.TDMASolverD(f_hat)
    u_hat = compute_vw(u_hat, f_hat, g_hat, K_over_K2)

    # Remains to fix wavenumber 0
    if rank == 0:
        w = work[((params.N[0], ), complex, 0)]
        w1 = work[((params.N[0], ), complex, 1, False)]

        h0_hat[0] = H_hat[1, :, 0, 0]
        h0_hat[1] = H_hat[2, :, 0, 0]

        h1[1, 0] = mat.BDD.matvec(h0_hat[0], h1[1, 0])
        h1[1, 1] = mat.BDD.matvec(h0_hat[1], h1[1, 1])
        h1[1, 0] -= Sk[1, :, 0, 0]  # Subtract constant pressure gradient

        beta = 2./params.nu/(a[rk]+b[rk])
        w[:] = beta*(a[rk]*h1[1, 0] + b[rk]*h1[0, 0])
        w += mat.ADD.matvec(u0_hat[0], w1)
        w += beta/params.dt*mat.BDD.matvec(u0_hat[0], w1)
        u0_hat[0] = la.HelmholtzSolverU0[rk](u0_hat[0], w)

        w[:] = beta*(a[rk]*h1[1, 1] + b[rk]*h1[0, 1])
        w += mat.ADD.matvec(u0_hat[1], w1)
        w += beta/params.dt*mat.BDD.matvec(u0_hat[1], w1)
        u0_hat[1] = la.HelmholtzSolverU0[rk](u0_hat[1], w)

        h1[0] = h1[1]

        u_hat[1, :, 0, 0] = u0_hat[0]
        u_hat[2, :, 0, 0] = u0_hat[1]

    return u_hat, g_hat

def integrate(u_hat, g_hat, rhs, dt, solver, context):
    """Three stage Runge Kutta integrator for KMM channel solver"""
    for rk in range(3):
        rhs = solver.ComputeRHS(rhs, u_hat, g_hat, rk, solver, **context)
        u_hat, g_hat = solver.solve_linear(u_hat, g_hat, rhs, rk, **context)

    return (u_hat, g_hat), dt, dt
