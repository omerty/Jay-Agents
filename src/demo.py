"""CLI — discover, process imported contacts, or manual prospect mode."""

import argparse
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent import run_discover_workflow, run_process_imported, run_requalify_all, run_workflow
from .db import export_csv, stats
from .llm import LLMError, ensure_llm

console = Console()


def _print_qualification(qual: dict, mode_label: str):
    table = Table(title="ICP Qualification")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Score", f"{qual['score']}/100")
    table.add_row("Tier", qual["tier"].upper())
    table.add_row("Mode", qual.get("mode", mode_label))
    table.add_row("Industries", ", ".join(qual["industries"]) or "—")
    table.add_row("Title", qual["title"] or "—")
    table.add_row("Recommendation", qual["recommendation"])
    console.print(table)

    if qual.get("reasons"):
        console.print("\n[bold]Reasons:[/bold]")
        for r in qual["reasons"]:
            console.print(f"  • {r}")

    if qual.get("talking_points"):
        console.print("\n[bold]Talking points:[/bold]")
        for t in qual["talking_points"]:
            console.print(f"  • {t}")


def _print_contact(contact: dict):
    if not contact:
        return
    lines = []
    if contact.get("contact_name"):
        lines.append(f"Name: {contact['contact_name']}")
    if contact.get("contact_title"):
        lines.append(f"Title: {contact['contact_title']}")
    if contact.get("email"):
        lines.append(f"Email: {contact['email']}")
    if lines:
        console.print(Panel("\n".join(lines), title="Contact", border_style="cyan"))


def _print_outreach(outreach: dict | None):
    if not outreach:
        return
    title = f"Draft Outreach ({outreach['mode']})"
    if outreach.get("to_email"):
        title += f" → {outreach['to_email']}"
    console.print()
    console.print(Panel(outreach["body"], title=title, border_style="green"))


def _run_single(args, use_llm: bool, mode_label: str):
    with console.status("[bold blue]Researching & qualifying…[/bold blue]"):
        result = run_workflow(
            args.agent, args.prospect,
            use_llm=use_llm, use_research=not args.no_research,
        )

    config = result["agent"]
    console.print()
    console.print(Panel(
        f"[bold]{config['company']}[/bold] — {config['product']}\n{config['tagline']}",
        title=f"Agent: {config['name']} ({mode_label})",
        border_style="blue",
    ))
    console.print(f"\n[bold]Prospect:[/bold] {args.prospect}\n")
    _print_contact(result.get("contact"))
    _print_qualification(result["qualification"], mode_label)

    if result["qualification"]["score"] >= 50 and result.get("outreach"):
        _print_outreach(result["outreach"])
    else:
        console.print("\n[yellow]Score below 50 — no outreach generated.[/yellow]")


def _run_discover(args, use_llm: bool, mode_label: str):
    with console.status("[bold blue]Scavenging web for ICP-fit companies…[/bold blue]"):
        result = run_discover_workflow(
            args.agent, limit=args.limit, use_llm=use_llm,
            draft_outreach_for_top=not args.no_outreach,
        )

    config = result["agent"]
    discovery = result["discovery"]
    qualified = discovery["qualified"]
    skipped = result.get("skipped_duplicates", 0)

    console.print()
    console.print(Panel(
        f"[bold]{config['company']}[/bold] — {config['product']}\n"
        f"{len(discovery['hits'])} search hits · {len(discovery['leads'])} extracted · "
        f"{skipped} skipped (already in DB)",
        title=f"Agent: {config['name']} — Discover ({mode_label})",
        border_style="blue",
    ))

    s = stats(agent=args.agent)
    console.print(f"[dim]DB: {s['total']} leads ({s['with_email']} with email)[/dim]\n")

    if not qualified:
        console.print("[yellow]No new qualified prospects. All may be in DB already.[/yellow]")
        return

    table = Table(title=f"New Prospects (top {len(qualified)})")
    table.add_column("#", style="dim")
    table.add_column("Company", style="bold")
    table.add_column("Contact")
    table.add_column("Score")
    table.add_column("Tier")
    table.add_column("Signal", max_width=35)

    for i, lead in enumerate(qualified, 1):
        q = lead["qualification"]
        tier_style = {"hot": "red", "warm": "yellow", "cold": "dim"}.get(q["tier"], "white")
        contact = lead.get("contact_name") or "—"
        table.add_row(
            str(i), lead["company"], contact,
            f"{q['score']}/100",
            f"[{tier_style}]{q['tier'].upper()}[/{tier_style}]",
            (lead.get("signal") or "")[:35],
        )
    console.print(table)

    best = qualified[0]
    console.print(f"\n[bold]Top prospect:[/bold] {best['prospect']}\n")
    _print_qualification(best["qualification"], mode_label)
    _print_outreach(result.get("top_outreach") or best.get("outreach"))


def _run_requalify_all(args, use_llm: bool, mode_label: str):
    with console.status("[bold blue]Re-qualifying existing leads with Groq…[/bold blue]"):
        result = run_requalify_all(
            args.agent,
            limit=args.limit,
            use_llm=use_llm,
            use_research=not args.no_research,
            draft_outreach=not args.no_outreach,
            on_progress=lambda m: console.print(f"[dim]{m}[/dim]"),
        )

    config = result["agent"]
    processed = result["processed"]

    console.print()
    console.print(Panel(
        f"[bold]{config['company']}[/bold] — {config['product']}\n"
        f"Re-qualified {result['count']} leads with {mode_label}",
        title=f"Agent: {config['name']} — Re-qualify",
        border_style="blue",
    ))

    if not processed:
        console.print("\n[yellow]No leads to re-qualify.[/yellow]")
        return

    table = Table(title="Re-qualified Leads")
    table.add_column("Company", style="bold")
    table.add_column("Contact")
    table.add_column("Score")
    table.add_column("Tier")
    table.add_column("Status")

    for row in processed:
        q = row["qualification"]
        tier_style = {"hot": "red", "warm": "yellow", "cold": "dim"}.get(q["tier"], "white")
        table.add_row(
            row.get("company", "—"),
            row.get("contact_name") or "—",
            f"{q['score']}/100",
            f"[{tier_style}]{q['tier'].upper()}[/{tier_style}]",
            row.get("status", "—"),
        )
    console.print()
    console.print(table)

    best = processed[0]
    console.print(f"\n[bold]Top lead:[/bold] {best['prospect']}\n")
    _print_qualification(best["qualification"], mode_label)
    _print_outreach(best.get("outreach"))


def _run_process_imported(args, use_llm: bool, mode_label: str):
    with console.status("[bold blue]Processing imported contacts…[/bold blue]"):
        result = run_process_imported(args.agent, limit=args.limit, use_llm=use_llm)

    config = result["agent"]
    processed = result["processed"]

    console.print()
    console.print(Panel(
        f"[bold]{config['company']}[/bold] — {config['product']}\n"
        f"Processed {result['count']} imported contacts",
        title=f"Agent: {config['name']} — Contacts ({mode_label})",
        border_style="blue",
    ))

    if not processed:
        console.print("\n[yellow]No imported leads pending.[/yellow]")
        console.print("Search first: [bold]python -m src.pdl_cli search --limit 25[/bold]\n")
        return

    table = Table(title="Processed Contacts")
    table.add_column("Company", style="bold")
    table.add_column("Contact")
    table.add_column("Email")
    table.add_column("Score")
    table.add_column("Tier")

    for row in processed:
        q = row["qualification"]
        tier_style = {"hot": "red", "warm": "yellow", "cold": "dim"}.get(q["tier"], "white")
        table.add_row(
            row.get("company", "—"),
            row.get("contact_name") or "—",
            row.get("email") or "—",
            f"{q['score']}/100",
            f"[{tier_style}]{q['tier'].upper()}[/{tier_style}]",
        )
    console.print()
    console.print(table)

    best = processed[0]
    console.print(f"\n[bold]Top contact:[/bold] {best['prospect']}\n")
    _print_contact(best.get("contact"))
    _print_qualification(best["qualification"], mode_label)
    _print_outreach(best.get("outreach"))


def main():
    parser = argparse.ArgumentParser(
        description="JayAgents — prospecting agents (woodway, fonex, keira)",
        epilog=(
            "Workflow: python -m src.pdl_cli search → --process-imported → --export\n"
            "Or: run with no flags to auto-discover new companies."
        ),
    )
    parser.add_argument("agent", choices=["woodway", "fonex", "keira"])
    parser.add_argument("--prospect", "-p", help="Manual: qualify one prospect")
    parser.add_argument(
        "--process-imported", action="store_true",
        help="Qualify + draft outreach for imported contacts (PDL or CSV)",
    )
    parser.add_argument(
        "--requalify-all", action="store_true",
        help="Re-run qualify + outreach for all existing leads (skips emailed/replied)",
    )
    parser.add_argument("--limit", "-n", type=int, default=5)
    parser.add_argument("--export", metavar="FILE", help="Export all leads to CSV and exit")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--no-research", action="store_true")
    parser.add_argument("--no-outreach", action="store_true")
    args = parser.parse_args()

    if args.export:
        n = export_csv(args.export, agent=args.agent)
        console.print(f"\n[green]Exported {n} leads to {args.export}[/green]\n")
        return

    use_llm = not args.mock
    mode_label = "mock"
    if use_llm:
        try:
            status = ensure_llm()
            mode_label = f"{status['provider']}:{status['model']}"
            console.print(f"[dim]LLM connected — {mode_label}[/dim]")
        except LLMError as e:
            console.print(f"[red]LLM error:[/red] {e}")
            console.print("[dim]Tip: set GROQ_API_KEY (free) or OPENAI_API_KEY in .env, "
                          "or run Ollama locally. Use --mock to skip the LLM.[/dim]")
            sys.exit(1)

    if args.requalify_all:
        _run_requalify_all(args, use_llm, mode_label)
    elif args.process_imported:
        _run_process_imported(args, use_llm, mode_label)
    elif args.prospect:
        _run_single(args, use_llm, mode_label)
    else:
        _run_discover(args, use_llm, mode_label)

    console.print()


if __name__ == "__main__":
    main()
