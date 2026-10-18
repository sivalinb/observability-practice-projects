#!/usr/bin/env python3
"""Optional Gradio controls that execute the actual project code and show evidence."""
from pathlib import Path
import json
import atexit
import gradio as gr
from shared.runner import Telemetry, execute, projects

TELEMETRY = None


def run_project(identifier):
    global TELEMETRY
    if TELEMETRY is None:
        TELEMETRY = Telemetry()
        atexit.register(TELEMETRY.shutdown)
    report = execute(identifier, TELEMETRY)
    TELEMETRY.flush()
    count = sum(c["passed"] for c in report["checks"])
    rows = [f"## {report['title']}", report["summary"], "", f"**{count}/{len(report['checks'])} checks passed** · Actual run `{report['run_id']}`", "", "| Measurement | Actual value |", "| --- | --- |"]
    rows.extend(f"| {name.replace('_', ' ')} | {value} |" for name, value in report.get("measurements", {}).items())
    rows += ["", "### Executed scenarios", ""]
    rows.extend(f"- {'✓' if c['passed'] else '✕'} **{c['name']}** — {c.get('detail', '')}" for c in report["checks"])
    rows += ["", "The values above come from this execution. Use Grafana for the shared stack's metrics, logs and traces."]
    return str(projects()[identifier] / "assets/architecture.webp"), "\n".join(rows), report


def build():
    choices = [(p.name[:2] + " · " + p.name[3:].replace("-", " ").title(), identifier) for identifier, p in projects().items()]
    with gr.Blocks(title="Observability Practice Projects", theme=gr.themes.Soft()) as app:
        gr.Markdown("# Observability Practice Projects\nRun a real Python demonstration, inspect its measured results, and explore the architectural decisions. Each execution saves raw artifacts. No cloud API is used by default.")
        with gr.Row():
            choice = gr.Dropdown(choices=choices, value="01", label="Choose a practice project")
            button = gr.Button("Run the project", variant="primary")
        with gr.Row():
            illustration = gr.Image(label="Illustrated architecture story", interactive=False)
            results = gr.Markdown("Select a project and run it to see actual results.")
        with gr.Accordion("Raw execution evidence", open=False):
            raw = gr.JSON()
        button.click(run_project, choice, [illustration, results, raw], concurrency_limit=1)
    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17860)
    args = parser.parse_args()
    build().queue(default_concurrency_limit=1).launch(server_name="127.0.0.1", server_port=args.port, share=False, allowed_paths=[str(Path(__file__).resolve().parent / "projects")])
