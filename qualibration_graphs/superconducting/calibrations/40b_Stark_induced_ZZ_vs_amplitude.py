# %% {Imports}
import matplotlib.pyplot as plt
import numpy as np
import xarray as xr
from dataclasses import asdict

from qm.qua import *

from qualang_tools.loops import from_array
from qualang_tools.multi_user import qm_session
from qualang_tools.results import progress_counter
from qualang_tools.units import unit

from qualibrate import QualibrationNode
from quam_config import Quam
from calibration_utils.stark_zz_vs_amplitude import (
    Parameters,
    process_raw_dataset,
    fit_raw_data,
    log_fitted_results,
    plot_raw_data_with_fit,
)
from calibration_utils.cr_utils import *
from calibration_utils.data_process_utils import *
from qualibration_libs.parameters import get_qubit_pairs, get_qubits
from qualibration_libs.runtime import simulate_and_plot
from qualibration_libs.data import XarrayDataFetcher
from qualibration_libs.core import tracked_updates


# %% {Description}
description =  """
        Stark-induced ZZ
"""


# Be sure to include [Parameters, Quam] so the node has proper type hinting
node = QualibrationNode[Parameters, Quam](
    name="40b_Stark_induced_ZZ_vs_amplitude",  # Name should be unique
    description=description,  # Describe what the node is doing, which is also reflected in the QUAlibrate GUI
    parameters=Parameters(),  # Node parameters defined under quam_experiment/experiments/node_name
)


# Any parameters that should change for debugging purposes only should go in here
# These parameters are ignored when run through the GUI or as part of a graph
@node.run_action(skip_if=node.modes.external)
def custom_param(node: QualibrationNode[Parameters, Quam]):
    """Allow the user to locally set the node parameters for debugging purposes, or execution in the Python IDE."""
    # You can get type hinting in your IDE by typing node.parameters.
    node.parameters.qubit_pairs = ["q1-2", "q3-4"]
    node.parameters.use_state_discrimination = True

    node.parameters.wf_type = "square"
    # node.parameters.zz_control_amp_scalings = [0.5, 0.5]
    # node.parameters.zz_target_amp_scalings = [0.5, 0.5]
    node.parameters.zz_control_phases = [0.0, 0.0]
    node.parameters.zz_target_phases = [0.5, 0.5]

    node.parameters.min_zz_drive_amp_scaling = 0.2
    node.parameters.max_zz_drive_amp_scaling = 1.0
    node.parameters.step_zz_drive_amp_scaling = 0.2


# Instantiate the QUAM class from the state file
node.machine = Quam.load()


# %% {Create_QUA_program}
@node.run_action(skip_if=node.parameters.load_data_id is not None)
def create_qua_program(node: QualibrationNode[Parameters, Quam]):
    """Create the sweep axes and generate the QUA program from the pulse sequence and the node parameters."""
    # Class containing tools to help handle units and conversions.
    u = unit(coerce_to_integer=True)

    node.namespace["qubit_pairs"] = qubit_pairs = get_qubit_pairs(node)
    num_qubit_pairs = len(qubit_pairs)

    # Update the readout power to match the desired range, this change will be reverted at the end of the node.
    node.namespace["tracked_qubit_pairs"] = []
    for qp in qubit_pairs:
        with tracked_updates(qp, auto_revert=False) as qp:
            pass
        node.namespace["tracked_qubit_pairs"].append(qp)

    n_avg = node.parameters.num_shots  # The number of averages
    state_discrimination = node.parameters.use_state_discrimination
    wf_type = node.parameters.wf_type
    # zz_control_amp_scalings = node.parameters.zz_control_amp_scalings
    # zz_target_amp_scalings = node.parameters.zz_target_amp_scalings
    zz_control_phases = node.parameters.zz_control_phases
    zz_target_phases = node.parameters.zz_target_phases

    # Pulse amplitude sweep (as a pre-factor of the qubit pulse amplitude) - must be within [-2; 2)
    idle_times = np.arange(
        node.parameters.min_wait_time_in_ns // 4,
        node.parameters.max_wait_time_in_ns // 4,
        node.parameters.time_step_in_ns // 4,
    )
    amp_scalings = np.arange(
        node.parameters.min_zz_drive_amp_scaling,
        node.parameters.max_zz_drive_amp_scaling,
        node.parameters.step_zz_drive_amp_scaling,
    )

    # Control states
    control_state = np.array([0, 1])

    if not state_discrimination:
        raise ValueError("use_state_discrimination must be True for Hamiltonian Tomography!")

    # Register the sweep axes to be added to the dataset when fetching data
    node.namespace["sweep_axes"] = {
        "qubit_pair": xr.DataArray(qubit_pairs.get_names()),
        "amp_scaling": xr.DataArray(amp_scalings, attrs={"long_name": "stark zz drive amplitude scaling"}),
        "idle_time": xr.DataArray(4 * idle_times, attrs={"long_name": "idle times", "units": "ns"}),
        "control_state": xr.DataArray(control_state, attrs={"long_name": "control state"}),
    }

    with program() as node.namespace["qua_program"]:
        I_c, I_c_st, Q_c, Q_c_st, n, n_st = node.machine.declare_qua_variables(num_IQ_pairs=num_qubit_pairs)
        I_t, I_t_st, Q_t, Q_t_st, _, _ = node.machine.declare_qua_variables(num_IQ_pairs=num_qubit_pairs)
        if state_discrimination:
            state_c = [declare(int) for _ in range(num_qubit_pairs)]
            state_t = [declare(int) for _ in range(num_qubit_pairs)]
            state_c_st = [declare_stream() for _ in range(num_qubit_pairs)]
            state_t_st = [declare_stream() for _ in range(num_qubit_pairs)]
        t = declare(int)  # QUA variable for idle time
        s = declare(int)  # QUA variable for control state
        a = declare(float) # QUA variable for amplitude scaling

        # Reset explicitly
        reset_global_phase()
        
        for multiplexed_qubit_pairs in qubit_pairs.batch():
            # Initialize the QPU in terms of flux points (flux tunable transmons and/or tunable couplers)
            for qp in multiplexed_qubit_pairs.values():
                node.machine.initialize_qpu(target=qp.qubit_control)
                node.machine.initialize_qpu(target=qp.qubit_target)
            align()

            with for_(n, 0, n < n_avg, n + 1):
                save(n, n_st)

                with for_(*from_array(a, amp_scalings)):

                    with for_(*from_array(t, idle_times)):

                        with for_(s, 0, s < 2, s + 1): # states 0:g or 1:e

                            # Reset the qubits to the ground state
                            for i, qp in multiplexed_qubit_pairs.items():
                                qc = qp.qubit_control
                                qt = qp.qubit_target

                                # Reset the qubits to the ground state
                                qc.reset(
                                    node.parameters.reset_type,
                                    node.parameters.simulate,
                                    log_callable=node.log,
                                )
                                qt.reset(
                                    node.parameters.reset_type,
                                    node.parameters.simulate,
                                    log_callable=node.log,
                                )

                                # x90
                                qt.xy.play("x90")

                                # idle - echo
                                qt.wait(t)
                                qp.align()

                                # flip
                                with if_(s == 1):
                                    qc.xy.play("x180")
                                    qt.xy.play("x180")
                                with if_(s == 0):
                                    qt.xy.play("x180")
                                qp.align()

                                # Play CZ
                                qp.apply("stark_cz",
                                    wf_type=wf_type,
                                    zz_control_amp_scaling=a,
                                    zz_target_amp_scaling=a,
                                    # zz_control_phase=zz_control_phases[i],
                                    # zz_target_phase=zz_target_phases[i],
                                    zz_duration_clock_cycles=t,
                                )                                
                                qp.align()

                                # idle - echo
                                qt.wait(t)                                
                                qp.align()

                                # x90
                                qt.xy.play('x90')
                                qp.align()

                                # Measure the state of the resonators
                                if state_discrimination:
                                    qc.readout_state(state_c[i])
                                    qt.readout_state(state_t[i])
                                    save(state_c[i], state_c_st[i])
                                    save(state_t[i], state_t_st[i])
                                else:
                                    qc.resonator.measure("readout", qua_vars=(I_c[i], Q_c[i]))
                                    qt.resonator.measure("readout", qua_vars=(I_t[i], Q_t[i]))
                                    # save data
                                    save(I_c[i], I_c_st[i])
                                    save(Q_c[i], Q_c_st[i])
                                    save(I_t[i], I_t_st[i])
                                    save(Q_t[i], Q_t_st[i])

                                # Reset the frame of the qubits in order not to accumulate rotations
                                reset_frame(qt.xy.name)
                                reset_frame(qt.xy_detuned.name)

                                # Wait for the qubit to decay to the ground state - Can be replaced by active reset
                                qc.resonator.wait(qc.resonator.depletion_time * u.ns)
                                qp.qubit_target.resonator.wait(qp.qubit_target.resonator.depletion_time * u.ns)

        with stream_processing():
            n_st.save("n")
            for i, qp in enumerate(qubit_pairs):
                if state_discrimination:
                    state_c_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"state_c{i + 1}")
                    state_t_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"state_t{i + 1}")
                else:
                    I_c_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"I_c{i + 1}")
                    Q_c_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"Q_c{i + 1}")
                    I_t_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"I_t{i + 1}")
                    Q_t_st[i].buffer(2).buffer(len(idle_times)).buffer(len(amp_scalings)).average().save(f"Q_t{i + 1}")


# %% {Simulate}
@node.run_action(skip_if=node.parameters.load_data_id is not None or not node.parameters.simulate)
def simulate_qua_program(node: QualibrationNode[Parameters, Quam]):
    """Connect to the QOP and simulate the QUA program"""
    # Connect to the QOP
    qmm = node.machine.connect()
    # Get the config from the machine
    config = node.machine.generate_config()

    # Simulate the QUA program, generate the waveform report and plot the simulated samples
    samples, fig, wf_report = simulate_and_plot(qmm, config, node.namespace["qua_program"], node.parameters)
    # Store the figure, waveform report and simulated samples
    node.results["simulation"] = {"figure": fig, "wf_report": wf_report, "samples": samples}


# %% {Execute}
@node.run_action(skip_if=node.parameters.load_data_id is not None or node.parameters.simulate)
def execute_qua_program(node: QualibrationNode[Parameters, Quam]):
    """Connect to the QOP, execute the QUA program and fetch the raw data and store it in a xarray dataset called "ds_raw"."""
    # Connect to the QOP
    qmm = node.machine.connect()
    # Get the config from the machine
    config = node.machine.generate_config()

    # Execute the QUA program only if the quantum machine is available (this is to avoid interrupting running jobs).
    with qm_session(qmm, config, timeout=node.parameters.timeout) as qm:
        # The job is stored in the node namespace to be reused in the fetching_data run_action
        node.namespace["job"] = job = qm.execute(node.namespace["qua_program"])
        # Display the progress bar
        data_fetcher = XarrayDataFetcher(job, node.namespace["sweep_axes"])
        for dataset in data_fetcher:
            progress_counter(
                data_fetcher["n"],
                node.parameters.num_shots,
                start_time=data_fetcher.t_start,
            )
        # Display the execution report to expose possible runtime errors
        node.log(job.execution_report())

    # Register the raw dataset
    node.results["ds_raw"] = dataset


# %% {Load_data}
@node.run_action(skip_if=node.parameters.load_data_id is None)
def load_data(node: QualibrationNode[Parameters, Quam]):
    """Load a previously acquired dataset."""
    load_data_id = node.parameters.load_data_id
    # Load the specified dataset
    node.load_from_id(node.parameters.load_data_id)
    node.parameters.load_data_id = load_data_id
    # Get the active qubits from the loaded node parameters
    node.namespace["qubit_pairs"] = get_qubit_pairs(node)


# %% {Analyse_data}
@node.run_action(skip_if=node.parameters.simulate)
def analyse_data(node: QualibrationNode[Parameters, Quam]):
    """Analyse the raw data and store the fitted data in another xarray dataset "ds_fit" and the fitted results in the "fit_results" dictionary."""
    node.results["ds_raw"] = process_raw_dataset(node.results["ds_raw"], node)
    print("kaka")
    node.results["ds_fit"], fit_results = fit_raw_data(node.results["ds_raw"], node)
    node.results["fit_results"] = {k: asdict(v) for k, v in fit_results.items()}

    # # Log the relevant information extracted from the data analysis
    log_fitted_results(node.results["fit_results"], log_callable=node.log)
    node.outcomes = {
        qubit_name: ("successful" if fit_result["success"] else "failed")
        for qubit_name, fit_result in node.results["fit_results"].items()
    }


# %% {Plot_data}
@node.run_action(skip_if=node.parameters.simulate)
def plot_data(node: QualibrationNode[Parameters, Quam]):
    """Plot the raw and fitted data in specific figures whose shape is given by qubit.grid_location."""
    figs_raw_fit = plot_raw_data_with_fit(node.results["ds_raw"], node.namespace["qubit_pairs"], node.results["ds_fit"])
    plt.show()
    # Store the generated figures
    node.results["figures"] = {
        f"IQ_{qp.name}": fig
        for fig, qp in zip(figs_raw_fit, node.namespace["qubit_pairs"])
    }


# %% {Update_state}
@node.run_action(skip_if=node.parameters.simulate)
def update_state(node: QualibrationNode[Parameters, Quam]):
    """Update the relevant parameters if the qubit data analysis was successful."""

    # Revert the change done at the beginning of the node
    for tracked_qubit_pair in node.namespace.get("tracked_qubit_pairs", []):
        tracked_qubit_pair.zz_drive.revert_changes()
        tracked_qubit_pair.qubit_control.revert_changes()
        tracked_qubit_pair.qubit_target.revert_changes()

    with node.record_state_updates():

        # TODO: manually pick the value and update
        print("TODO: manually pick the amplitude scaling and update the amplitude of ZZ drive pulses!")
        zz_control_amp_scaling_fitted = [1.0, 1.0]
        zz_target_amp_scaling_fitted = [1.0, 1.0]

        for i, qp in enumerate(node.namespace["qubit_pairs"]):
            if node.outcomes[qp.name] == "failed":
                continue

            # zz control
            operation_c = qp.zz_drive.operations[node.parameters.wf_type]
            operation_c.amplitude = zz_control_amp_scaling_fitted[i] * operation_c.amplitude
            # zz target
            operation_t = qp.qubit_target.xy.operations[f"cr_{node.parameters.wf_type}_{qp.name}"]
            operation_t.amplitude = zz_target_amp_scaling_fitted[i] * operation_t.amplitude


# %% {Save_results}
@node.run_action()
def save_results(node: QualibrationNode[Parameters, Quam]):
    node.save()

# %%
