/*
 * thermal_rc2 -- an FMI 3.0 Co-Simulation FMU of a generic two-node thermal
 * RC network, written for the Scientific-AI harness.
 *
 *   C1 dT1/dt = Q - G12 (T1 - T2)
 *   C2 dT2/dt = G12 (T1 - T2) - G2a (T2 - Tamb)
 *
 * Inputs Q [W] and Tamb [K] are held constant over a communication step
 * (zero-order hold). The state is advanced by classical fourth-order
 * Runge-Kutta with n_sub equal substeps per communication step, so the
 * step-size dependence of a result is a property of a stated integrator
 * (global error O(h^4)), not of an unstated one.
 *
 * The FMU also integrates the heat that entered (Q) and the heat that left
 * through G2a, so an importer can check the energy balance
 *   C1 (T1 - T1_0) + C2 (T2 - T2_0) = E_in - E_out
 * from the outputs alone.
 *
 * NON_AUTHORITATIVE. This is a simulator. Its outputs are SIMULATED
 * observations; nothing it produces is a measurement, and nothing crosses
 * the FMI boundary as authority (see the <Annotations> of
 * modelDescription.xml).
 *
 * No global state: every instance owns its memory, so two instances in one
 * process do not interact. FMU state (fmi3GetFMUState / fmi3SetFMUState /
 * fmi3SerializeFMUState) is the full dynamic state plus the communication
 * time; restoring it reproduces the trajectory exactly, because RK4 with a
 * fixed substep is a deterministic function of (state, inputs, step).
 */
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "fmi3Functions.h"

#define MODEL_IDENTIFIER_STR "thermal_rc2"

/* NEGATIVE CONTROL ONLY. Built with -DTHERMAL_RC2_FAULT=1 the dynamics use a
 * conductance G12 10 % larger than the one the FMU reports and the
 * description declares: a wrong model that looks right from outside. The
 * harness's verification must reject it; tools/fmi_build.py --fault builds
 * it under a different archive name, and nothing imports it as the model. */
#ifdef THERMAL_RC2_FAULT
#define G12_DYNAMICS_FACTOR 1.10
#else
#define G12_DYNAMICS_FACTOR 1.0
#endif
#define INSTANTIATION_TOKEN "{6f7a1b52-2c1e-4d5e-9a0b-5c3e7b1d2f40}"

/* value references -- must agree with modelDescription.xml */
enum {
    VR_TIME = 0,
    VR_C1 = 1, VR_C2 = 2, VR_G12 = 3, VR_G2A = 4,
    VR_T1_0 = 5, VR_T2_0 = 6,
    VR_Q = 7, VR_TAMB = 8,
    VR_T1 = 9, VR_T2 = 10, VR_E_IN = 11, VR_E_OUT = 12, VR_E_STORED = 13,
    VR_N_SUB = 14,
    VR_COUNT = 15
};

typedef enum { ST_INSTANTIATED, ST_INIT, ST_STEP, ST_TERMINATED } Phase;

typedef struct {
    /* parameters */
    double C1, C2, G12, G2a, T1_0, T2_0;
    int32_t n_sub;
    /* inputs */
    double Q, Tamb;
    /* state */
    double t, T1, T2, E_in, E_out;
} Model;

typedef struct {
    Model m;
    Phase phase;
    fmi3InstanceEnvironment env;
    fmi3LogMessageCallback log;
    int logging;
    char *name;
} Instance;

typedef struct {
    Model m;
    Phase phase;
} SavedState;

#define SERIAL_MAGIC 0x52433246u  /* "RC2F" */
#define SERIAL_VERSION 1u
/* magic, version, phase, n_sub, then 13 doubles */
#define SERIAL_SIZE (4 * sizeof(uint32_t) + 13 * sizeof(double))

static void logf_(Instance *c, fmi3Status st, const char *cat,
                  const char *fmt, ...) {
    char buf[512];
    va_list ap;
    if (!c || !c->log) return;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    c->log(c->env, st, cat, buf);
}

static int finite_positive(double x) { return isfinite(x) && x > 0.0; }

static fmi3Status check_parameters(Instance *c) {
    Model *m = &c->m;
    if (!finite_positive(m->C1) || !finite_positive(m->C2) ||
        !finite_positive(m->G12) || !finite_positive(m->G2a)) {
        logf_(c, fmi3Error, "logStatusError",
              "C1, C2, G12 and G2a must be finite and positive");
        return fmi3Error;
    }
    if (!finite_positive(m->T1_0) || !finite_positive(m->T2_0)) {
        logf_(c, fmi3Error, "logStatusError",
              "initial temperatures must be finite and positive (kelvin)");
        return fmi3Error;
    }
    if (m->n_sub < 1 || m->n_sub > 100000) {
        logf_(c, fmi3Error, "logStatusError", "n_sub must be in [1, 100000]");
        return fmi3Error;
    }
    return fmi3OK;
}

static fmi3Status check_inputs(Instance *c) {
    if (!isfinite(c->m.Q) || !finite_positive(c->m.Tamb)) {
        logf_(c, fmi3Error, "logStatusError",
              "Q must be finite and Tamb finite and positive");
        return fmi3Error;
    }
    return fmi3OK;
}

static void rhs(const Model *m, double T1, double T2, double *d1,
                double *d2, double *qin, double *qout) {
    double q12 = m->G12 * G12_DYNAMICS_FACTOR * (T1 - T2);
    double q2a = m->G2a * (T2 - m->Tamb);
    *d1 = (m->Q - q12) / m->C1;
    *d2 = (q12 - q2a) / m->C2;
    *qin = m->Q;
    *qout = q2a;
}

/* one RK4 step of length h on (T1, T2, E_in, E_out) */
static void rk4(Model *m, double h) {
    double k1a, k1b, k1i, k1o, k2a, k2b, k2i, k2o;
    double k3a, k3b, k3i, k3o, k4a, k4b, k4i, k4o;
    double T1 = m->T1, T2 = m->T2;
    rhs(m, T1, T2, &k1a, &k1b, &k1i, &k1o);
    rhs(m, T1 + 0.5 * h * k1a, T2 + 0.5 * h * k1b, &k2a, &k2b, &k2i, &k2o);
    rhs(m, T1 + 0.5 * h * k2a, T2 + 0.5 * h * k2b, &k3a, &k3b, &k3i, &k3o);
    rhs(m, T1 + h * k3a, T2 + h * k3b, &k4a, &k4b, &k4i, &k4o);
    m->T1 = T1 + h / 6.0 * (k1a + 2.0 * k2a + 2.0 * k3a + k4a);
    m->T2 = T2 + h / 6.0 * (k1b + 2.0 * k2b + 2.0 * k3b + k4b);
    m->E_in += h / 6.0 * (k1i + 2.0 * k2i + 2.0 * k3i + k4i);
    m->E_out += h / 6.0 * (k1o + 2.0 * k2o + 2.0 * k3o + k4o);
}

static double stored(const Model *m) {
    return m->C1 * (m->T1 - m->T1_0) + m->C2 * (m->T2 - m->T2_0);
}

static void defaults(Model *m) {
    m->C1 = 500.0; m->C2 = 2000.0; m->G12 = 5.0; m->G2a = 2.0;
    m->T1_0 = 300.0; m->T2_0 = 300.0; m->n_sub = 4;
    m->Q = 10.0; m->Tamb = 300.0;
    m->t = 0.0; m->T1 = m->T1_0; m->T2 = m->T2_0;
    m->E_in = 0.0; m->E_out = 0.0;
}

/* ---- FMI 3.0 API -------------------------------------------------------- */

const char *fmi3GetVersion(void) { return fmi3Version; }

fmi3Status fmi3SetDebugLogging(fmi3Instance instance, fmi3Boolean on,
                               size_t n, const fmi3String categories[]) {
    (void)n; (void)categories;
    ((Instance *)instance)->logging = on ? 1 : 0;
    return fmi3OK;
}

fmi3Instance fmi3InstantiateModelExchange(
    fmi3String a, fmi3String b, fmi3String c, fmi3Boolean d, fmi3Boolean e,
    fmi3InstanceEnvironment f, fmi3LogMessageCallback g) {
    (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g;
    return NULL;  /* Co-Simulation only */
}

fmi3Instance fmi3InstantiateScheduledExecution(
    fmi3String a, fmi3String b, fmi3String c, fmi3Boolean d, fmi3Boolean e,
    fmi3InstanceEnvironment f, fmi3LogMessageCallback g,
    fmi3ClockUpdateCallback h, fmi3LockPreemptionCallback i,
    fmi3UnlockPreemptionCallback j) {
    (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g; (void)h;
    (void)i; (void)j;
    return NULL;
}

fmi3Instance fmi3InstantiateCoSimulation(
    fmi3String instanceName, fmi3String instantiationToken,
    fmi3String resourcePath, fmi3Boolean visible, fmi3Boolean loggingOn,
    fmi3Boolean eventModeUsed, fmi3Boolean earlyReturnAllowed,
    const fmi3ValueReference requiredIntermediateVariables[],
    size_t nRequiredIntermediateVariables,
    fmi3InstanceEnvironment instanceEnvironment,
    fmi3LogMessageCallback logMessage,
    fmi3IntermediateUpdateCallback intermediateUpdate) {
    Instance *c;
    (void)resourcePath; (void)visible; (void)requiredIntermediateVariables;
    (void)intermediateUpdate;
    if (!instantiationToken ||
        strcmp(instantiationToken, INSTANTIATION_TOKEN) != 0) {
        if (logMessage)
            logMessage(instanceEnvironment, fmi3Error, "logStatusError",
                       "instantiation token does not match this binary");
        return NULL;
    }
    if (eventModeUsed || earlyReturnAllowed ||
        nRequiredIntermediateVariables != 0) {
        if (logMessage)
            logMessage(instanceEnvironment, fmi3Error, "logStatusError",
                       "event mode, early return and intermediate updates "
                       "are not supported");
        return NULL;
    }
    c = (Instance *)calloc(1, sizeof(Instance));
    if (!c) return NULL;
    {
        const char *nm = instanceName ? instanceName : "";
        size_t len = strlen(nm) + 1;
        c->name = (char *)malloc(len);
        if (!c->name) { free(c); return NULL; }
        memcpy(c->name, nm, len);
    }
    c->env = instanceEnvironment;
    c->log = logMessage;
    c->logging = loggingOn ? 1 : 0;
    c->phase = ST_INSTANTIATED;
    defaults(&c->m);
    return c;
}

void fmi3FreeInstance(fmi3Instance instance) {
    Instance *c = (Instance *)instance;
    if (!c) return;
    free(c->name);
    free(c);
}

fmi3Status fmi3EnterInitializationMode(fmi3Instance instance,
                                       fmi3Boolean toleranceDefined,
                                       fmi3Float64 tolerance,
                                       fmi3Float64 startTime,
                                       fmi3Boolean stopTimeDefined,
                                       fmi3Float64 stopTime) {
    Instance *c = (Instance *)instance;
    (void)toleranceDefined; (void)tolerance; (void)stopTimeDefined;
    (void)stopTime;
    if (c->phase != ST_INSTANTIATED) return fmi3Error;
    if (!isfinite(startTime)) return fmi3Error;
    c->m.t = startTime;
    c->phase = ST_INIT;
    return fmi3OK;
}

fmi3Status fmi3ExitInitializationMode(fmi3Instance instance) {
    Instance *c = (Instance *)instance;
    if (c->phase != ST_INIT) return fmi3Error;
    if (check_parameters(c) != fmi3OK || check_inputs(c) != fmi3OK)
        return fmi3Error;
    c->m.T1 = c->m.T1_0;
    c->m.T2 = c->m.T2_0;
    c->m.E_in = 0.0;
    c->m.E_out = 0.0;
    c->phase = ST_STEP;
    return fmi3OK;
}

fmi3Status fmi3EnterEventMode(fmi3Instance instance) {
    (void)instance;
    return fmi3Error;
}

fmi3Status fmi3Terminate(fmi3Instance instance) {
    Instance *c = (Instance *)instance;
    if (c->phase != ST_STEP && c->phase != ST_INIT) return fmi3Error;
    c->phase = ST_TERMINATED;
    return fmi3OK;
}

fmi3Status fmi3Reset(fmi3Instance instance) {
    Instance *c = (Instance *)instance;
    defaults(&c->m);
    c->phase = ST_INSTANTIATED;
    return fmi3OK;
}

/* variables that may be set in a phase */
static int settable(const Instance *c, fmi3ValueReference vr) {
    switch (vr) {
    case VR_C1: case VR_C2: case VR_G12: case VR_G2A: case VR_T1_0:
    case VR_T2_0: case VR_N_SUB:
        return c->phase == ST_INSTANTIATED || c->phase == ST_INIT;
    case VR_Q: case VR_TAMB:
        return c->phase == ST_INSTANTIATED || c->phase == ST_INIT ||
               c->phase == ST_STEP;
    default:
        return 0;
    }
}

fmi3Status fmi3GetFloat64(fmi3Instance instance,
                          const fmi3ValueReference vr[], size_t nvr,
                          fmi3Float64 values[], size_t nValues) {
    Instance *c = (Instance *)instance;
    Model *m = &c->m;
    size_t i;
    if (nValues != nvr) return fmi3Error;
    for (i = 0; i < nvr; i++) {
        switch (vr[i]) {
        case VR_TIME: values[i] = m->t; break;
        case VR_C1: values[i] = m->C1; break;
        case VR_C2: values[i] = m->C2; break;
        case VR_G12: values[i] = m->G12; break;
        case VR_G2A: values[i] = m->G2a; break;
        case VR_T1_0: values[i] = m->T1_0; break;
        case VR_T2_0: values[i] = m->T2_0; break;
        case VR_Q: values[i] = m->Q; break;
        case VR_TAMB: values[i] = m->Tamb; break;
        case VR_T1: values[i] = m->T1; break;
        case VR_T2: values[i] = m->T2; break;
        case VR_E_IN: values[i] = m->E_in; break;
        case VR_E_OUT: values[i] = m->E_out; break;
        case VR_E_STORED: values[i] = stored(m); break;
        default:
            logf_(c, fmi3Error, "logStatusError",
                  "no Float64 variable with value reference %u", vr[i]);
            return fmi3Error;
        }
    }
    return fmi3OK;
}

fmi3Status fmi3SetFloat64(fmi3Instance instance,
                          const fmi3ValueReference vr[], size_t nvr,
                          const fmi3Float64 values[], size_t nValues) {
    Instance *c = (Instance *)instance;
    Model *m = &c->m;
    size_t i;
    if (nValues != nvr) return fmi3Error;
    for (i = 0; i < nvr; i++) {
        if (!settable(c, vr[i]) || vr[i] == VR_N_SUB) {
            logf_(c, fmi3Error, "logStatusError",
                  "value reference %u may not be set now", vr[i]);
            return fmi3Error;
        }
        if (!isfinite(values[i])) {
            logf_(c, fmi3Error, "logStatusError",
                  "non-finite value for value reference %u", vr[i]);
            return fmi3Error;
        }
        switch (vr[i]) {
        case VR_C1: m->C1 = values[i]; break;
        case VR_C2: m->C2 = values[i]; break;
        case VR_G12: m->G12 = values[i]; break;
        case VR_G2A: m->G2a = values[i]; break;
        case VR_T1_0: m->T1_0 = values[i]; break;
        case VR_T2_0: m->T2_0 = values[i]; break;
        case VR_Q: m->Q = values[i]; break;
        case VR_TAMB: m->Tamb = values[i]; break;
        default: return fmi3Error;
        }
    }
    if (c->phase == ST_STEP) return check_inputs(c);
    return fmi3OK;
}

fmi3Status fmi3GetInt32(fmi3Instance instance,
                        const fmi3ValueReference vr[], size_t nvr,
                        fmi3Int32 values[], size_t nValues) {
    Instance *c = (Instance *)instance;
    size_t i;
    if (nValues != nvr) return fmi3Error;
    for (i = 0; i < nvr; i++) {
        if (vr[i] != VR_N_SUB) return fmi3Error;
        values[i] = c->m.n_sub;
    }
    return fmi3OK;
}

fmi3Status fmi3SetInt32(fmi3Instance instance,
                        const fmi3ValueReference vr[], size_t nvr,
                        const fmi3Int32 values[], size_t nValues) {
    Instance *c = (Instance *)instance;
    size_t i;
    if (nValues != nvr) return fmi3Error;
    for (i = 0; i < nvr; i++) {
        if (vr[i] != VR_N_SUB || !settable(c, vr[i])) return fmi3Error;
        c->m.n_sub = values[i];
    }
    return fmi3OK;
}

fmi3Status fmi3DoStep(fmi3Instance instance,
                      fmi3Float64 currentCommunicationPoint,
                      fmi3Float64 communicationStepSize,
                      fmi3Boolean noSetFMUStatePriorToCurrentPoint,
                      fmi3Boolean *eventHandlingNeeded,
                      fmi3Boolean *terminateSimulation,
                      fmi3Boolean *earlyReturn,
                      fmi3Float64 *lastSuccessfulTime) {
    Instance *c = (Instance *)instance;
    Model *m = &c->m;
    double h;
    int32_t k;
    (void)noSetFMUStatePriorToCurrentPoint;
    if (eventHandlingNeeded) *eventHandlingNeeded = fmi3False;
    if (terminateSimulation) *terminateSimulation = fmi3False;
    if (earlyReturn) *earlyReturn = fmi3False;
    if (lastSuccessfulTime) *lastSuccessfulTime = m->t;
    if (c->phase != ST_STEP) return fmi3Error;
    if (!isfinite(communicationStepSize) || communicationStepSize <= 0.0) {
        logf_(c, fmi3Error, "logStatusError",
              "communication step size must be finite and positive");
        return fmi3Error;
    }
    /* A step must start where the FMU is: a caller that skips or repeats
     * time is refused rather than silently re-based. */
    if (fabs(currentCommunicationPoint - m->t) >
        1e-12 * fmax(1.0, fabs(m->t))) {
        logf_(c, fmi3Error, "logStatusError",
              "communication point %.17g is not the FMU time %.17g",
              currentCommunicationPoint, m->t);
        return fmi3Error;
    }
    if (check_inputs(c) != fmi3OK) return fmi3Error;
    h = communicationStepSize / (double)m->n_sub;
    for (k = 0; k < m->n_sub; k++) rk4(m, h);
    m->t = currentCommunicationPoint + communicationStepSize;
    if (!isfinite(m->T1) || !isfinite(m->T2)) {
        logf_(c, fmi3Error, "logStatusError", "state became non-finite");
        return fmi3Error;
    }
    if (lastSuccessfulTime) *lastSuccessfulTime = m->t;
    return fmi3OK;
}

/* ---- FMU state ----------------------------------------------------------- */

fmi3Status fmi3GetFMUState(fmi3Instance instance, fmi3FMUState *FMUState) {
    Instance *c = (Instance *)instance;
    SavedState *s;
    if (!FMUState) return fmi3Error;
    s = *FMUState ? (SavedState *)*FMUState
                  : (SavedState *)malloc(sizeof(SavedState));
    if (!s) return fmi3Error;
    s->m = c->m;
    s->phase = c->phase;
    *FMUState = s;
    return fmi3OK;
}

fmi3Status fmi3SetFMUState(fmi3Instance instance, fmi3FMUState FMUState) {
    Instance *c = (Instance *)instance;
    SavedState *s = (SavedState *)FMUState;
    if (!s) return fmi3Error;
    c->m = s->m;
    c->phase = s->phase;
    return fmi3OK;
}

fmi3Status fmi3FreeFMUState(fmi3Instance instance, fmi3FMUState *FMUState) {
    (void)instance;
    if (FMUState && *FMUState) {
        free(*FMUState);
        *FMUState = NULL;
    }
    return fmi3OK;
}

fmi3Status fmi3SerializedFMUStateSize(fmi3Instance instance,
                                      fmi3FMUState FMUState, size_t *size) {
    (void)instance; (void)FMUState;
    *size = SERIAL_SIZE;
    return fmi3OK;
}

static void put_u32(fmi3Byte **p, uint32_t v) {
    memcpy(*p, &v, sizeof v); *p += sizeof v;
}
static void put_f64(fmi3Byte **p, double v) {
    memcpy(*p, &v, sizeof v); *p += sizeof v;
}
static uint32_t get_u32(const fmi3Byte **p) {
    uint32_t v; memcpy(&v, *p, sizeof v); *p += sizeof v; return v;
}
static double get_f64(const fmi3Byte **p) {
    double v; memcpy(&v, *p, sizeof v); *p += sizeof v; return v;
}

fmi3Status fmi3SerializeFMUState(fmi3Instance instance,
                                 fmi3FMUState FMUState,
                                 fmi3Byte serializedState[], size_t size) {
    SavedState *s = (SavedState *)FMUState;
    fmi3Byte *p = serializedState;
    const Model *m;
    (void)instance;
    if (!s || size != SERIAL_SIZE) return fmi3Error;
    m = &s->m;
    put_u32(&p, SERIAL_MAGIC);
    put_u32(&p, SERIAL_VERSION);
    put_u32(&p, (uint32_t)s->phase);
    put_u32(&p, (uint32_t)m->n_sub);
    put_f64(&p, m->C1); put_f64(&p, m->C2); put_f64(&p, m->G12);
    put_f64(&p, m->G2a); put_f64(&p, m->T1_0); put_f64(&p, m->T2_0);
    put_f64(&p, m->Q); put_f64(&p, m->Tamb); put_f64(&p, m->t);
    put_f64(&p, m->T1); put_f64(&p, m->T2); put_f64(&p, m->E_in);
    put_f64(&p, m->E_out);
    return fmi3OK;
}

fmi3Status fmi3DeserializeFMUState(fmi3Instance instance,
                                   const fmi3Byte serializedState[],
                                   size_t size, fmi3FMUState *FMUState) {
    Instance *c = (Instance *)instance;
    const fmi3Byte *p = serializedState;
    SavedState tmp, *s;
    uint32_t phase, n_sub;
    if (size != SERIAL_SIZE || !serializedState) return fmi3Error;
    if (get_u32(&p) != SERIAL_MAGIC || get_u32(&p) != SERIAL_VERSION) {
        logf_(c, fmi3Error, "logStatusError",
              "serialized state is not a thermal_rc2 v1 state");
        return fmi3Error;
    }
    phase = get_u32(&p);
    n_sub = get_u32(&p);
    if (phase > (uint32_t)ST_TERMINATED || n_sub < 1 || n_sub > 100000)
        return fmi3Error;
    tmp.phase = (Phase)phase;
    tmp.m.n_sub = (int32_t)n_sub;
    tmp.m.C1 = get_f64(&p); tmp.m.C2 = get_f64(&p); tmp.m.G12 = get_f64(&p);
    tmp.m.G2a = get_f64(&p); tmp.m.T1_0 = get_f64(&p);
    tmp.m.T2_0 = get_f64(&p); tmp.m.Q = get_f64(&p);
    tmp.m.Tamb = get_f64(&p); tmp.m.t = get_f64(&p); tmp.m.T1 = get_f64(&p);
    tmp.m.T2 = get_f64(&p); tmp.m.E_in = get_f64(&p);
    tmp.m.E_out = get_f64(&p);
    if (!isfinite(tmp.m.t) || !isfinite(tmp.m.T1) || !isfinite(tmp.m.T2) ||
        !isfinite(tmp.m.E_in) || !isfinite(tmp.m.E_out)) {
        logf_(c, fmi3Error, "logStatusError",
              "serialized state holds a non-finite value");
        return fmi3Error;
    }
    s = (SavedState *)malloc(sizeof(SavedState));
    if (!s) return fmi3Error;
    *s = tmp;
    *FMUState = s;
    return fmi3OK;
}

/* ---- everything else this FMU does not support -------------------------- */

#define UNSUPPORTED_GETSET(NAME, T)                                          \
    fmi3Status fmi3Get##NAME(fmi3Instance instance,                         \
                             const fmi3ValueReference vr[], size_t nvr,     \
                             T values[], size_t nValues) {                  \
        (void)instance; (void)vr; (void)nvr; (void)values; (void)nValues;  \
        return fmi3Error;                                                   \
    }                                                                       \
    fmi3Status fmi3Set##NAME(fmi3Instance instance,                         \
                             const fmi3ValueReference vr[], size_t nvr,     \
                             const T values[], size_t nValues) {            \
        (void)instance; (void)vr; (void)nvr; (void)values; (void)nValues;  \
        return fmi3Error;                                                   \
    }

UNSUPPORTED_GETSET(Float32, fmi3Float32)
UNSUPPORTED_GETSET(Int8, fmi3Int8)
UNSUPPORTED_GETSET(UInt8, fmi3UInt8)
UNSUPPORTED_GETSET(Int16, fmi3Int16)
UNSUPPORTED_GETSET(UInt16, fmi3UInt16)
UNSUPPORTED_GETSET(UInt32, fmi3UInt32)
UNSUPPORTED_GETSET(Int64, fmi3Int64)
UNSUPPORTED_GETSET(UInt64, fmi3UInt64)
UNSUPPORTED_GETSET(Boolean, fmi3Boolean)
UNSUPPORTED_GETSET(String, fmi3String)

fmi3Status fmi3GetBinary(fmi3Instance instance,
                         const fmi3ValueReference vr[], size_t nvr,
                         size_t valueSizes[], fmi3Binary values[],
                         size_t nValues) {
    (void)instance; (void)vr; (void)nvr; (void)valueSizes; (void)values;
    (void)nValues;
    return fmi3Error;
}

fmi3Status fmi3SetBinary(fmi3Instance instance,
                         const fmi3ValueReference vr[], size_t nvr,
                         const size_t valueSizes[], const fmi3Binary values[],
                         size_t nValues) {
    (void)instance; (void)vr; (void)nvr; (void)valueSizes; (void)values;
    (void)nValues;
    return fmi3Error;
}

fmi3Status fmi3GetClock(fmi3Instance i, const fmi3ValueReference v[],
                        size_t n, fmi3Clock values[]) {
    (void)i; (void)v; (void)n; (void)values; return fmi3Error;
}
fmi3Status fmi3SetClock(fmi3Instance i, const fmi3ValueReference v[],
                        size_t n, const fmi3Clock values[]) {
    (void)i; (void)v; (void)n; (void)values; return fmi3Error;
}
fmi3Status fmi3GetNumberOfVariableDependencies(
    fmi3Instance i, fmi3ValueReference v, size_t *n) {
    (void)i; (void)v; (void)n; return fmi3Error;
}
fmi3Status fmi3GetVariableDependencies(
    fmi3Instance i, fmi3ValueReference d, size_t e[],
    fmi3ValueReference f[], size_t g[], fmi3DependencyKind h[], size_t n) {
    (void)i; (void)d; (void)e; (void)f; (void)g; (void)h; (void)n;
    return fmi3Error;
}
fmi3Status fmi3GetDirectionalDerivative(
    fmi3Instance i, const fmi3ValueReference a[], size_t b,
    const fmi3ValueReference c[], size_t d, const fmi3Float64 e[], size_t f,
    fmi3Float64 g[], size_t h) {
    (void)i; (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g;
    (void)h; return fmi3Error;
}
fmi3Status fmi3GetAdjointDerivative(
    fmi3Instance i, const fmi3ValueReference a[], size_t b,
    const fmi3ValueReference c[], size_t d, const fmi3Float64 e[], size_t f,
    fmi3Float64 g[], size_t h) {
    (void)i; (void)a; (void)b; (void)c; (void)d; (void)e; (void)f; (void)g;
    (void)h; return fmi3Error;
}
fmi3Status fmi3EnterConfigurationMode(fmi3Instance i) {
    (void)i; return fmi3Error;
}
fmi3Status fmi3ExitConfigurationMode(fmi3Instance i) {
    (void)i; return fmi3Error;
}
fmi3Status fmi3GetIntervalDecimal(fmi3Instance i,
                                  const fmi3ValueReference v[], size_t n,
                                  fmi3Float64 a[],
                                  fmi3IntervalQualifier b[]) {
    (void)i; (void)v; (void)n; (void)a; (void)b; return fmi3Error;
}
fmi3Status fmi3GetIntervalFraction(fmi3Instance i,
                                   const fmi3ValueReference v[], size_t n,
                                   fmi3UInt64 a[], fmi3UInt64 b[],
                                   fmi3IntervalQualifier c[]) {
    (void)i; (void)v; (void)n; (void)a; (void)b; (void)c; return fmi3Error;
}
fmi3Status fmi3GetShiftDecimal(fmi3Instance i, const fmi3ValueReference v[],
                               size_t n, fmi3Float64 a[]) {
    (void)i; (void)v; (void)n; (void)a; return fmi3Error;
}
fmi3Status fmi3GetShiftFraction(fmi3Instance i, const fmi3ValueReference v[],
                                size_t n, fmi3UInt64 a[], fmi3UInt64 b[]) {
    (void)i; (void)v; (void)n; (void)a; (void)b; return fmi3Error;
}
fmi3Status fmi3SetIntervalDecimal(fmi3Instance i,
                                  const fmi3ValueReference v[], size_t n,
                                  const fmi3Float64 a[]) {
    (void)i; (void)v; (void)n; (void)a; return fmi3Error;
}
fmi3Status fmi3SetIntervalFraction(fmi3Instance i,
                                   const fmi3ValueReference v[], size_t n,
                                   const fmi3UInt64 a[],
                                   const fmi3UInt64 b[]) {
    (void)i; (void)v; (void)n; (void)a; (void)b; return fmi3Error;
}
fmi3Status fmi3SetShiftDecimal(fmi3Instance i, const fmi3ValueReference v[],
                               size_t n, const fmi3Float64 a[]) {
    (void)i; (void)v; (void)n; (void)a; return fmi3Error;
}
fmi3Status fmi3SetShiftFraction(fmi3Instance i, const fmi3ValueReference v[],
                                size_t n, const fmi3UInt64 a[],
                                const fmi3UInt64 b[]) {
    (void)i; (void)v; (void)n; (void)a; (void)b; return fmi3Error;
}
fmi3Status fmi3EvaluateDiscreteStates(fmi3Instance i) {
    (void)i; return fmi3Error;
}
fmi3Status fmi3UpdateDiscreteStates(fmi3Instance i, fmi3Boolean *a,
                                    fmi3Boolean *b, fmi3Boolean *c,
                                    fmi3Boolean *d, fmi3Boolean *e,
                                    fmi3Float64 *f) {
    (void)i; (void)a; (void)b; (void)c; (void)d; (void)e; (void)f;
    return fmi3Error;
}
fmi3Status fmi3EnterContinuousTimeMode(fmi3Instance i) {
    (void)i; return fmi3Error;
}
fmi3Status fmi3CompletedIntegratorStep(fmi3Instance i, fmi3Boolean a,
                                       fmi3Boolean *b, fmi3Boolean *c) {
    (void)i; (void)a; (void)b; (void)c; return fmi3Error;
}
fmi3Status fmi3SetTime(fmi3Instance i, fmi3Float64 t) {
    (void)i; (void)t; return fmi3Error;
}
fmi3Status fmi3SetContinuousStates(fmi3Instance i, const fmi3Float64 x[],
                                   size_t n) {
    (void)i; (void)x; (void)n; return fmi3Error;
}
fmi3Status fmi3GetContinuousStateDerivatives(fmi3Instance i,
                                             fmi3Float64 d[], size_t n) {
    (void)i; (void)d; (void)n; return fmi3Error;
}
fmi3Status fmi3GetEventIndicators(fmi3Instance i, fmi3Float64 e[],
                                  size_t n) {
    (void)i; (void)e; (void)n; return fmi3Error;
}
fmi3Status fmi3GetContinuousStates(fmi3Instance i, fmi3Float64 x[],
                                   size_t n) {
    (void)i; (void)x; (void)n; return fmi3Error;
}
fmi3Status fmi3GetNominalsOfContinuousStates(fmi3Instance i,
                                             fmi3Float64 x[], size_t n) {
    (void)i; (void)x; (void)n; return fmi3Error;
}
fmi3Status fmi3GetNumberOfEventIndicators(fmi3Instance i, size_t *n) {
    (void)i; (void)n; return fmi3Error;
}
fmi3Status fmi3GetNumberOfContinuousStates(fmi3Instance i, size_t *n) {
    (void)i; (void)n; return fmi3Error;
}
fmi3Status fmi3EnterStepMode(fmi3Instance i) {
    (void)i; return fmi3Error;
}
fmi3Status fmi3GetOutputDerivatives(fmi3Instance i,
                                    const fmi3ValueReference v[], size_t n,
                                    const fmi3Int32 o[], fmi3Float64 x[],
                                    size_t m) {
    (void)i; (void)v; (void)n; (void)o; (void)x; (void)m; return fmi3Error;
}
fmi3Status fmi3ActivateModelPartition(fmi3Instance i, fmi3ValueReference c,
                                      fmi3Float64 t) {
    (void)i; (void)c; (void)t; return fmi3Error;
}
