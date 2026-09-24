# Deployment with a user-provided EDA backend

AetherPilot separates public Python code from the local EDA installation. The repository never supplies a product installation, process-design kit, model files, license material, or a workspace database. A user or an automation agent provides those values in a local configuration file that is not committed.

## 1. Install the public package

Use a Python environment supported by the target installation and install the project or a built wheel. A wheel can be installed without the source tree; a source install requires a normal Python build environment with a recent setuptools and wheel backend.

```bash
python -m pip install .
python -m aetherpilot --version
```

## 2. Create a private configuration

Copy `config.example.json` to a local file outside the committed tree. Fill in `backend` (`native` or `auto`), the product environment script, an isolated workspace, an artifact output directory, the local technology root, and any model library files required by the selected simulator. Relative paths are resolved from the configuration file location.

The configuration contains references, not copies, of the product, technology, model, and license resources. Do not put license text, host identifiers, model source, or absolute paths into an issue, pull request, or public artifact.

## 3. Check the backend

```bash
python -m aetherpilot backend-status --config path/to/local-config.json
python -m aetherpilot preflight --config path/to/local-config.json
```

`backend-status` performs a static check for the configured product interpreter and Python package tree. It does not source the environment script, start a license service, open a GUI, or modify a workspace. `preflight` uses the configured backend and reports a machine-readable failure when the product, Python package, license, workspace, or technology is unavailable.

## 4. Run an operation

Pass the same configuration explicitly to the command or set `AETHERPILOT_CONFIG` in the private shell. Native operations run in the configured workspace and write operation evidence under the configured artifact directory. Offline parsing, validation, readback comparison, and result normalization can run with `backend=offline` and do not require the external EDA installation.

## 5. Agent handoff clues

An automation agent should first locate a product environment entry point, confirm the product Python executable and PyAether import location, identify a writable isolated workspace, identify the technology root and model files, and verify the license through the user's normal site procedure. It should then generate a local configuration file, run `backend-status`, run `preflight`, and only after both succeed call a native operation. The agent must not guess a technology path, copy a license, or treat a stub/backend-unavailable result as a successful native run.
