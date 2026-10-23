"""Independent finite-element computations for the Scientific-AI harness.

RUNS ONLY IN THE ISOLATED dolfinx ENVIRONMENT. It imports nothing from this
repository: it reads one JSON request on argv[1] and writes one JSON result
on argv[2], so the primary finite-volume implementation, its discretisation
and its operators cannot leak into the check. What the two share is what an
independent check may share: the problem's parameters and units, carried in
the request.

Computations:

* ``mms_steady``    -- -k lap(u) = f on the unit square, u exact; L2 error
                       on a refinement sequence for a given element degree.
* ``mms_transient`` -- rho c u_t = k u_xx + f on [0, 1] with Crank-Nicolson
                       and dt proportional to h; L2 error at t_end.
* ``slab``          -- the generic slab problem on a 2D rectangle whose top
                       and bottom are insulated, so the solution must reduce
                       to the 1D slab: P1, Crank-Nicolson, Robin at x = L.
                       Returns T at requested x positions (on the mid-line),
                       and the energy terms assembled from the FEM solution.

Every linear solve is a direct LU (PETSc), so a repeat on the same build and
input is expected to agree to round-off; whether it agrees to the bit is
measured and reported, never assumed.
"""
from __future__ import annotations

import hashlib
import json
import sys

import numpy as np
from mpi4py import MPI
from petsc4py import PETSc

import basix
import dolfinx
import ufl
from dolfinx import fem, geometry, mesh
from dolfinx.fem.petsc import LinearProblem

COMM = MPI.COMM_SELF
SOLVER = {"ksp_type": "preonly", "pc_type": "lu",
          "pc_factor_mat_solver_type": "petsc"}


def _problem(a, L, bcs=(), prefix="h"):
    return LinearProblem(a, L, bcs=list(bcs), petsc_options=SOLVER,
                         petsc_options_prefix=f"{prefix}_")


def _l2(u_h, u_exact_expr, degree_raise=3):
    V = u_h.function_space
    msh = V.mesh
    dx = ufl.Measure("dx", domain=msh,
                     metadata={"quadrature_degree":
                               V.ufl_element().basix_element.degree
                               + degree_raise + 2})
    err = fem.form((u_h - u_exact_expr) ** 2 * dx)
    return float(np.sqrt(COMM.allreduce(fem.assemble_scalar(err), op=MPI.SUM)))


def _mesh_digest(msh) -> str:
    x = np.ascontiguousarray(msh.geometry.x, dtype=np.float64)
    c = np.ascontiguousarray(
        msh.topology.connectivity(msh.topology.dim, 0).array, dtype=np.int64)
    return hashlib.sha256(x.tobytes() + c.tobytes()).hexdigest()


def mms_steady(req: dict) -> dict:
    k = float(req["k"])
    deg = int(req["degree"])
    # a NEGATIVE CONTROL may build the source with a wrong conductivity;
    # the study must then fail to converge at the a-priori order
    k_source = k * float(req.get("source_k_factor", 1.0))
    out = []
    for n in req["n"]:
        msh = mesh.create_unit_square(COMM, int(n), int(n),
                                      mesh.CellType.triangle)
        V = fem.functionspace(msh, ("Lagrange", deg))
        x = ufl.SpatialCoordinate(msh)
        u_ex = 1.0 + ufl.sin(ufl.pi * x[0]) * ufl.sin(2 * ufl.pi * x[1]) \
            + x[0] * x[1]
        f = -k_source * ufl.div(ufl.grad(u_ex))
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        a = k * ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx
        L = f * v * ufl.dx
        msh.topology.create_connectivity(1, 2)
        facets = mesh.exterior_facet_indices(msh.topology)
        dofs = fem.locate_dofs_topological(V, 1, facets)
        ubc = fem.Function(V)
        ubc.interpolate(fem.Expression(u_ex, V.element.interpolation_points))
        bc = fem.dirichletbc(ubc, dofs)
        uh = _problem(a, L, [bc], prefix=f"mms{deg}_{n}").solve()
        out.append({"n": int(n), "h": 1.0 / int(n),
                    "l2_error": _l2(uh, u_ex),
                    "mesh_sha256": _mesh_digest(msh)})
    return {"kind": "mms_steady", "degree": deg, "levels": out}


def mms_transient(req: dict) -> dict:
    k, rc = float(req["k"]), float(req["rho_c"])
    t_end = float(req["t_end"])
    out = []
    for n in req["n"]:
        msh = mesh.create_interval(COMM, int(n), [0.0, 1.0])
        V = fem.functionspace(msh, ("Lagrange", 1))
        x = ufl.SpatialCoordinate(msh)
        steps = int(n) * int(req["steps_per_cell"])
        dt = t_end / steps
        t = fem.Constant(msh, PETSc.ScalarType(0.0))

        def exact(tt):
            return ufl.exp(-tt) * ufl.cos(ufl.pi * x[0]) + x[0] ** 2

        def source(tt):
            ue = exact(tt)
            return rc * (-ufl.exp(-tt) * ufl.cos(ufl.pi * x[0])) \
                - k * ufl.div(ufl.grad(ue))

        u_n = fem.Function(V)
        u_n.interpolate(fem.Expression(exact(t),
                                       V.element.interpolation_points))
        # Neumann data from the exact solution: -k du/dn at both ends
        n_vec = ufl.FacetNormal(msh)
        u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
        t_old = fem.Constant(msh, PETSc.ScalarType(0.0))
        t_new = fem.Constant(msh, PETSc.ScalarType(dt))
        a = (rc * u * v + 0.5 * dt * k * ufl.inner(ufl.grad(u),
                                                    ufl.grad(v))) * ufl.dx
        Lf = (rc * u_n * v
              - 0.5 * dt * k * ufl.inner(ufl.grad(u_n), ufl.grad(v))
              + 0.5 * dt * (source(t_old) + source(t_new)) * v) * ufl.dx \
            + 0.5 * dt * k * (ufl.inner(ufl.grad(exact(t_old)), n_vec)
                              + ufl.inner(ufl.grad(exact(t_new)), n_vec)) \
            * v * ufl.ds
        prob = _problem(a, Lf, prefix=f"mmst_{n}")
        for i in range(steps):
            t_old.value = i * dt
            t_new.value = (i + 1) * dt
            uh = prob.solve()
            u_n.x.array[:] = uh.x.array
        t.value = t_end
        out.append({"n": int(n), "h": 1.0 / int(n), "dt": dt,
                    "l2_error": _l2(u_n, exact(t))})
    return {"kind": "mms_transient", "levels": out}


def slab(req: dict) -> dict:
    p = req["params"]
    L, W = float(p["L_m"]), float(p["width_m"])
    k, rc = float(p["k_W_m_K"]), float(p["rho_c_J_m3_K"])
    q, hcoef = float(p["q_W_m3"]), float(p["h_W_m2_K"])
    T_inf, T0 = float(p["T_inf_K"]), float(p["T0_K"])
    t_end, steps = float(p["t_end_s"]), int(req["steps"])
    nx, ny = int(req["nx"]), int(req["ny"])
    msh = mesh.create_rectangle(COMM, [np.array([0.0, 0.0]),
                                       np.array([L, W])], [nx, ny],
                                mesh.CellType.quadrilateral)
    V = fem.functionspace(msh, ("Lagrange", 1))
    fdim = msh.topology.dim - 1
    right = mesh.locate_entities_boundary(
        msh, fdim, lambda x: np.isclose(x[0], L))
    tags = mesh.meshtags(msh, fdim, right, np.full(right.size, 1,
                                                   dtype=np.int32))
    ds = ufl.Measure("ds", domain=msh, subdomain_data=tags)
    dt = t_end / steps
    u_n = fem.Function(V)
    u_n.x.array[:] = T0
    u, v = ufl.TrialFunction(V), ufl.TestFunction(V)
    kc, rcc = fem.Constant(msh, k), fem.Constant(msh, rc)
    qc, hc = fem.Constant(msh, q), fem.Constant(msh, hcoef)
    Tinf = fem.Constant(msh, T_inf)
    a = (rcc * u * v
         + 0.5 * dt * kc * ufl.inner(ufl.grad(u), ufl.grad(v))) * ufl.dx \
        + 0.5 * dt * hc * u * v * ds(1)
    Lf = (rcc * u_n * v
          - 0.5 * dt * kc * ufl.inner(ufl.grad(u_n), ufl.grad(v))
          + dt * qc * v) * ufl.dx \
        + (dt * hc * Tinf - 0.5 * dt * hc * u_n) * v * ds(1)
    prob = _problem(a, Lf, prefix=f"slab_{nx}")
    # energy accounting per unit depth, from the FEM fields themselves
    u_prev = fem.Function(V)
    E_out = 0.0
    flux_form_prev = fem.form(hc * (u_n - Tinf) * ds(1))
    for _ in range(steps):
        u_prev.x.array[:] = u_n.x.array
        f_prev = fem.assemble_scalar(flux_form_prev)
        uh = prob.solve()
        u_n.x.array[:] = uh.x.array
        f_new = fem.assemble_scalar(flux_form_prev)
        E_out += 0.5 * dt * (f_prev + f_new)
    U_end = fem.assemble_scalar(fem.form(rcc * (u_n - T0) * ufl.dx))
    E_in = q * L * W * t_end
    # sample along the mid-line y = W / 2
    xs = np.asarray(req["x_samples"], dtype=float)
    pts = np.zeros((xs.size, 3))
    pts[:, 0] = xs
    pts[:, 1] = 0.5 * W
    tree = geometry.bb_tree(msh, msh.topology.dim)
    cand = geometry.compute_collisions_points(tree, pts)
    cells = geometry.compute_colliding_cells(msh, cand, pts)
    vals = []
    for i in range(xs.size):
        c = cells.links(i)
        vals.append(float(u_n.eval(pts[i:i + 1], c[:1])[0]))
    # lateral uniformity: the 2D field must not vary across y (insulated)
    pts2 = pts.copy()
    pts2[:, 1] = 0.1 * W
    cand2 = geometry.compute_collisions_points(tree, pts2)
    cells2 = geometry.compute_colliding_cells(msh, cand2, pts2)
    lateral = max(abs(float(u_n.eval(pts2[i:i + 1],
                                     cells2.links(i)[:1])[0]) - vals[i])
                  for i in range(xs.size))
    return {"kind": "slab", "nx": nx, "ny": ny, "steps": steps,
            "x": xs.tolist(), "T": vals,
            "lateral_max_abs_K": lateral,
            "energy": {"E_in_J_per_m": E_in, "E_out_J_per_m": E_out,
                       "dU_J_per_m": U_end,
                       "residual_rel": (E_in - E_out - U_end)
                       / max(abs(E_in), abs(U_end), 1e-30)},
            "mesh_sha256": _mesh_digest(msh),
            "field_sha256": hashlib.sha256(
                np.ascontiguousarray(u_n.x.array).tobytes()).hexdigest()}


def provenance() -> dict:
    return {"dolfinx": dolfinx.__version__, "basix": basix.__version__,
            "ufl": ufl.__version__,
            "petsc": ".".join(map(str, PETSc.Sys.getVersion())),
            "petsc_scalar": str(np.dtype(PETSc.ScalarType)),
            "mpi": MPI.Get_library_version().splitlines()[0].strip(),
            "mpi_size": COMM.Get_size(),
            "solver": dict(SOLVER), "numpy": np.__version__}


def main(argv) -> int:
    req = json.loads(open(argv[1], encoding="utf-8").read())
    fn = {"mms_steady": mms_steady, "mms_transient": mms_transient,
          "slab": slab}[req["kind"]]
    res = fn(req)
    res["provenance"] = provenance()
    res["request_sha256"] = hashlib.sha256(
        json.dumps(req, sort_keys=True).encode()).hexdigest()
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(res, fh, sort_keys=True, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
