import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"

PYTHON = VENV / (
    "Scripts/python.exe"
    if os.name == "nt"
    else "bin/python"
)

SCRIPTS = [
    "citygml_to_rdf.py",
    "footprint_extractor.py",
    "climate_data.py",
    "osm_enrichment.py",
    "svf_calculator.py",
    "clms_landcover.py",
    "terrain_dgm.py",
    "risk_assessment.py",
    "uhi_calibration.py",
    "risk_assessment.py",
    "subzone_grid.py",
    "queries_and_viz.py",
]


def format_duration(seconds: float) -> str:
    """Format elapsed seconds as a readable duration."""
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours >= 1:
        return (
            f"{int(hours)}h "
            f"{int(minutes)}m "
            f"{secs:.1f}s"
        )

    if minutes >= 1:
        return f"{int(minutes)}m {secs:.1f}s"

    return f"{secs:.1f}s"


def run(cmd, env=None) -> float:
    """Run a command and return its elapsed time in seconds."""
    print("\n$", " ".join(map(str, cmd)))

    start = time.perf_counter()

    try:
        subprocess.run(
            cmd,
            cwd=ROOT,
            check=True,
            env=env,
        )
    finally:
        elapsed = time.perf_counter() - start
        print(
            f"Completed in "
            f"{format_duration(elapsed)}"
        )

    return elapsed


def main():
    pipeline_start = time.perf_counter()
    script_times: list[tuple[str, float]] = []

    try:
        if not REQUIREMENTS.exists():
            raise FileNotFoundError(
                f"Could not find {REQUIREMENTS}. "
                "Create requirements.txt before running "
                "the pipeline."
            )

        if not PYTHON.exists():
            run([
                sys.executable,
                "-m",
                "venv",
                str(VENV),
            ])

        run([
            str(PYTHON),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--quiet",
            "pip",
        ])

        run([
            str(PYTHON),
            "-m",
            "pip",
            "install",
            "-r",
            str(REQUIREMENTS),
        ])

        env = os.environ.copy()
        env["UHI_PIPELINE_RUN"] = "1"

        for script in SCRIPTS:
            script_path = ROOT / script

            if not script_path.exists():
                raise FileNotFoundError(
                    f"Missing pipeline script: "
                    f"{script_path}"
                )

            elapsed = run(
                [str(PYTHON), str(script_path)],
                env=env,
            )

            script_times.append(
                (script, elapsed)
            )

        print("\n" + "=" * 60)
        print("Pipeline timing summary")
        print("=" * 60)

        for script, elapsed in script_times:
            print(
                f"{script:<28} "
                f"{format_duration(elapsed):>15}"
            )

        print("\nPipeline completed successfully.")

    finally:
        total_elapsed = (
            time.perf_counter() - pipeline_start
        )

        print(
            "\nTotal elapsed time: "
            f"{format_duration(total_elapsed)}"
        )


if __name__ == "__main__":
    main()
    