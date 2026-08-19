//! Selective Rust kernels for the QTA multiphysics layer.
//!
//! MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.
//!
//! Only one kind of kernel is admitted here: hot, numerically simple, and
//! reproducible *bit for bit* against the NumPy reference in
//! `qta_multiphysics.stack.rust_kernel`. That constraint drives every choice
//! below — the arithmetic is written in exactly the association order NumPy
//! uses, there is no reassociation, no fused-multiply-add, no fast-math, and
//! no parallel reduction. A kernel that cannot show bit parity on the
//! project's test vectors is not adopted, and the Python fallback stays in
//! force.

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::prelude::*;

/// Series-resistance (harmonic) face conductance for a finite-volume face.
///
/// `G = area / (d_left / k_left + d_right / k_right)`
///
/// The association order is fixed to match the NumPy expression exactly:
/// two divisions, one addition, one division. Any other order can differ in
/// the last ulp and would fail the parity check.
#[pyfunction]
fn face_conductance<'py>(
    py: Python<'py>,
    area: PyReadonlyArray1<'py, f64>,
    d_left: PyReadonlyArray1<'py, f64>,
    k_left: PyReadonlyArray1<'py, f64>,
    d_right: PyReadonlyArray1<'py, f64>,
    k_right: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let a = area.as_slice()?;
    let dl = d_left.as_slice()?;
    let kl = k_left.as_slice()?;
    let dr = d_right.as_slice()?;
    let kr = k_right.as_slice()?;
    let n = a.len();
    if dl.len() != n || kl.len() != n || dr.len() != n || kr.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "face_conductance: all inputs must share one length",
        ));
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        out.push(a[i] / (dl[i] / kl[i] + dr[i] / kr[i]));
    }
    Ok(PyArray1::from_vec_bound(py, out))
}

/// Temperature-dependent conductivity `k(T) = k0 * (T / T_ref)^exponent`.
///
/// Same rule: written as one division then one `powf` then one multiply, in
/// the order the NumPy reference evaluates it.
#[pyfunction]
fn conductivity_power_law<'py>(
    py: Python<'py>,
    temperature: PyReadonlyArray1<'py, f64>,
    k0: f64,
    t_ref: f64,
    exponent: f64,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let t = temperature.as_slice()?;
    let mut out = Vec::with_capacity(t.len());
    for &ti in t {
        out.push(k0 * (ti / t_ref).powf(exponent));
    }
    Ok(PyArray1::from_vec_bound(py, out))
}

#[pymodule]
fn qta_kernels(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__doc__", "Selective Rust kernels for QTA (bit-parity required).")?;
    m.add("KERNEL_CONTRACT", "bit_for_bit_parity_with_numpy_reference")?;
    m.add_function(wrap_pyfunction!(face_conductance, m)?)?;
    m.add_function(wrap_pyfunction!(conductivity_power_law, m)?)?;
    Ok(())
}
