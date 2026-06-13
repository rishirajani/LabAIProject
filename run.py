import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"

PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

SCRIPTS = [
    "citygml_to_rdf.py",
    "climate_data.py",
    "osm_enrichment.py",
    "svf_calculator.py",     # geometric SVF from LoD2 wall/roof surfaces
    "clms_landcover.py",     # CLMS tree canopy + imperviousness (10 m raster)
    "terrain_dgm.py",        # TPI-derived topographic exposure (DGM1 1 m)
    "risk_assessment.py",
    "uhi_calibration.py",    # calibrate ΔT formula from DWD stations; writes α/β to graph
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
    