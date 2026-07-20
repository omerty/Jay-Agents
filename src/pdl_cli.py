"""CLI for contact search (Apollo/PDL), CSV import, and lead DB."""

import argparse
import sys

from . import env  # noqa: F401 — load .env

from rich.console import Console
from rich.table import Table

from .contacts import resolve_contacts_provider, search_and_import_contacts
from .db import export_csv, get_leads, stats
from .pdl import import_csv

console = Console()


def cmd_search(args):
    provider = resolve_contacts_provider()
    console.print(f"[dim]Contact provider: {provider}[/dim]")
    try:
        result = search_and_import_contacts(
            agent=args.agent,
            limit=args.limit,
            skip_existing=not args.include_existing,
            on_progress=lambda msg: console.print(f"[dim]{msg}[/dim]"),
        )
    except Exception as e:
        console.print(f"\n[red]Contact search error:[/red]\n{e}\n")
        status_code = getattr(e, "status_code", None)
        if status_code == 401:
            console.print("[dim]Check your API key in .env[/dim]\n")
        elif status_code == 402:
            console.print("[dim]PDL free tier = 100 credits/month. Each contact = 1 credit.[/dim]\n")
        sys.exit(1)

    console.print(f"\n[green]{provider} search complete[/green]")
    console.print(f"  Total matches:         {result.get('total_available', 0):,}")
    console.print(f"  Fetched this run:      {result['searched']}")
    console.print(f"  Credits used:          ~{result['credits_used']}")
    console.print(f"  Imported to DB:        {result['imported']}")
    if "with_email" in result:
        console.print(f"  With email:            {result['with_email']}")
    console.print(f"  Updated:               {result['updated']}")
    console.print(f"  Skipped (duplicates):  {result['skipped']}")
    if result["errors"]:
        console.print(f"  [yellow]Errors: {len(result['errors'])}[/yellow]")
    console.print(f"\nNext: python -m src.demo {args.agent} --process-imported\n")


def cmd_import(args):
    result = import_csv(args.csv, agent=args.agent)
    console.print("\n[green]CSV import complete[/green]")
    console.print(f"  Imported: {result['imported']}")
    console.print(f"  Updated:  {result['updated']}")
    console.print(f"  Skipped:  {result['skipped']}")
    console.print(f"\nNext: python -m src.demo {args.agent} --process-imported\n")


def cmd_status(args):
    s = stats(agent=args.agent)
    console.print(f"\n[bold]Leads database[/bold] ({s['total']} total, {s['with_email']} with email)\n")
    if s["by_status"]:
        table = Table()
        table.add_column("Status")
        table.add_column("Count", justify="right")
        for status, count in sorted(s["by_status"].items()):
            table.add_row(status, str(count))
        console.print(table)

    recent = get_leads(agent=args.agent, limit=5)
    if recent:
        console.print("\n[bold]Recent leads:[/bold]")
        for lead in recent:
            contact = lead.get("contact_name") or "—"
            email = lead.get("email") or "—"
            console.print(
                f"  • {lead['company']} — {contact} ({email}) "
                f"[dim]{lead['status']}, score={lead.get('score') or '—'}[/dim]"
            )
    console.print()


def cmd_export(args):
    n = export_csv(args.output, agent=args.agent)
    if n:
        console.print(f"\n[green]Exported {n} leads to {args.output}[/green]\n")
    else:
        console.print("\n[yellow]No leads to export.[/yellow]\n")


def main():
    parser = argparse.ArgumentParser(
        description="JayAgents — contact search (Apollo/PDL) & lead DB",
        epilog="Search: python -m src.pdl_cli search --limit 25",
    )
    parser.add_argument("--agent", default="woodway", choices=["woodway", "fonex", "keira"])
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser(
        "search",
        help="Search for ICP contacts (APOLLO_API_KEY → Apollo free search, else PDL)",
    )
    p_search.add_argument("--limit", "-n", type=int, default=25)
    p_search.add_argument("--include-existing", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_import = sub.add_parser("import", help="Import contacts CSV")
    p_import.add_argument("csv")
    p_import.set_defaults(func=cmd_import)

    p_status = sub.add_parser("status", help="Show leads DB stats")
    p_status.set_defaults(func=cmd_status)

    p_export = sub.add_parser("export", help="Export leads to CSV")
    p_export.add_argument("output")
    p_export.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
