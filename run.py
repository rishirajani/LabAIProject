import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".myenv"
REQUIREMENTS = ROOT / "requirements.txt"

PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

SCRIPTS = [
    "citygml_to_rdf.py",
    "svf_calculator.py",   # geometric SVF from LoD2 wall/roof surfaces
    "climate_data.py",
    "osm_enrichment.py",
    "copernicus_enrichment.py",  # overwrites veg/imperviousness fractions with 10 m satellite data
    "risk_assessment.py",
    "uhi_calibration.py",  # calibrate ΔT formula from DWD stations; writes coefficients to graph
    "risk_assessment.py",  # re-run to apply calibrated ΔT to all building assessments
    "queries_and_viz.py",
]


def run(cmd, env=None):
    print("\n$", " ".join(map(str, cmd)))
    subprocess.run(cmd, cwd=ROOT, check=True, env=env)


def main():
    if not REQUIREMENTS.exists():
        raise FileNotFoundError(
            f"Could not find {REQUIREMENTS}. "
            "Create requirements.txt before running the pipeline."
        )

    if not PYTHON.exists():
        run([sys.executable, "-m", "venv", str(VENV)])

    run([str(PYTHON), "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    run([str(PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)])

    env = os.environ.copy()
    env["UHI_PIPELINE_RUN"] = "1"

    for script in SCRIPTS:
        script_path = ROOT / script
        if not script_path.exists():
            raise FileNotFoundError(f"Missing pipeline script: {script_path}")
        run([str(PYTHON), str(script_path)], env=env)

    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()