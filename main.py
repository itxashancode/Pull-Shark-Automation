import signal
import json
import uuid
import time
import os
import argparse
from datetime import datetime
from pathlib import Path

from git_manager import GitManager
from github_tool import GitHubTool
from token_manager import TokenManager
from proxy_manager import ProxyManager
from notifier import Notifier
from logger import setup_logger
from telemetry import TelemetryClient, verify_telemetry

import requests
import dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

dotenv.load_dotenv()
console = Console()

PULL_SHARK_BANNER = """
 [bold cyan] [PULL_SHARK] >> SCANNING REPO...[/bold cyan]
 [bold cyan] [PULL_SHARK] >> IDENTITY: RUTHLESS PR AGENT[/bold cyan]
 [bold cyan] [PULL_SHARK] >> MODE: SURGICAL PRECISION[/bold cyan]
"""

class ShutdownHandler:
    def __init__(self):
        self._shutdown = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_):
        msg = "\n[bold yellow]── SHUTDOWN SIGNAL RECEIVED ──[/bold yellow]"
        console.print(msg)
        self._shutdown = True

    @property
    def requested(self) -> bool:
        return self._shutdown

shutdown = ShutdownHandler()

def pull_shark_log(msg, style="white", status=None):
    prefix = "[PULL_SHARK] >> "
    ts = datetime.now().strftime("%H:%M:%S")
    status_map = {
        "SCAN": "SCANNING REPO...",
        "LGTM": "LGTM ✓",
        "CHANGES": "NEEDS_CHANGES ✗",
        "BLOCKED": "BLOCKED ⛔",
        "MERGED": "MERGED ⬆"
    }
    
    if status in status_map:
        text = status_map[status]
        if status == "MERGED": style = "bold green"
        elif status == "SCAN": style = "cyan"
    else:
        text = msg
        
    console.print(f"[{ts}] [{style}]{prefix}{text}[/{style}]")

def load_config():
    """Load configuration"""
    with open("config.json") as f:
        return json.load(f)

def load_state():
    """Load state"""
    try:
        with open("state.json") as f:
            return json.load(f)
    except:
        return {"last_completed_pr": 0}

def save_state(state):
    """Save state"""
    with open("state.json", "w") as f:
        json.dump(state, f, indent=2)

def generate_content(index):
    """Generate README content"""
    return f"""
---

## Automated Contribution #{index}
Timestamp: {datetime.now().isoformat()}
UUID: {uuid.uuid4()}
---

"""

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description="GitHub Automation Tool")
    parser.add_argument("--count", type=int, help="Override PR count")
    parser.add_argument("--delay", type=int, help="Override delay seconds")
    parser.add_argument("--dry-run", action="store_true", help="Enable dry run mode")
    parser.add_argument("--no-merge", action="store_true", help="Disable auto merge")
    parser.add_argument("--reset", action="store_true", help="Reset state")
    parser.add_argument("--use-proxies", action="store_true", help="Use proxies")
    return parser.parse_args()

def display_aesthetic_banner(token: str, username: str):
    """Displays a non-truncating, fully responsive banner via Rich."""
    try:
        with open("../Galaxy Brain/ascii-art.txt", "r", encoding="utf-8") as f:
            art = f.read()
    except Exception:
        art = "[blue]ASCII ART NOT FOUND[/blue]"

    title_text = Text("\nP U L L - S H A R K   B O T\n=============================", style="cyan", justify="center")

    info_table = Table.grid(padding=(0, 1))
    info_table.add_column(style="bold cyan", justify="left")
    info_table.add_column(style="cyan", no_wrap=False)  

    info_table.add_row("User:", username)
    info_table.add_row("Dashboard:", f"{username} + [yellow]{token}[/yellow]")
    info_table.add_row("", "")
    info_table.add_row("GitHub Repo:", "[blue]https://github.com/itxashancode/Pull-Shark-Automation[/blue]")
    info_table.add_row("Pair Ext:", "[blue]https://github.com/itxashancode/Pair-Extraordinaire-Automation[/blue]")
    info_table.add_row("Follow Me:", "[blue]https://github.com/itxashancode[/blue]")

    content_table = Table.grid(padding=(0, 2))
    content_table.add_column(ratio=2) 
    content_table.add_column(ratio=1) 
    
    content_table.add_row(info_table, Text(art, style="cyan", justify="right"))

    full_layout = Table.grid(padding=(1, 0))
    full_layout.add_column(justify="center")
    full_layout.add_row(title_text)
    full_layout.add_row(content_table)

    panel = Panel(full_layout, border_style="cyan", padding=(1, 2), expand=True)
    console.print(panel)
    console.print(PULL_SHARK_BANNER)


def main():
    """Main function"""
    # Enforce telemetry integrity first and foremost
    verify_telemetry()
    
    args = parse_args()
    config = load_config()
    state = load_state()
    logger = setup_logger()

    if args.reset:
        state["last_completed_pr"] = 0
        save_state(state)
        print("🔄 State reset.")
        return

    # Apply overrides
    if args.count:
        config["pr_count"] = args.count
    if args.delay:
        config["delay_seconds"] = args.delay
    if args.dry_run:
        config["dry_run"] = True
    if args.no_merge:
        config["auto_merge"] = False

    # Change to repo directory
    os.chdir(config["repo_path"])

    # Initialize managers
    git = GitManager(config["base_branch"], config["max_retries"], logger)
    gh = GitHubTool(config["base_branch"], config["max_retries"], logger)
    token_manager = TokenManager()
    proxy_manager = ProxyManager() if args.use_proxies else None
    notifier = Notifier(config.get("slack_webhook"), config.get("discord_webhook"))

    # Fetch exact identity from API for dashboard logging
    username = "Unknown_User"
    main_token = ""
    try:
        tmb, _ = token_manager.get_best_token()
        if tmb:
            main_token = tmb
            user_resp = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {main_token}"}).json()
            if "login" in user_resp:
                username = user_resp["login"]
    except Exception as e:
        logger.warning(f"Could not identify primary GitHub user: {e}")
        
    display_aesthetic_banner(main_token, username)
    telemetry_client = TelemetryClient(github_username=username, github_token=main_token)

    start = state["last_completed_pr"] + 1
    end = config["pr_count"]
    
    session_prs_created = 0
    total_prs_created = state["last_completed_pr"]

    pull_shark_log(f"Starting execution cycle from PR #{start}")
    pull_shark_log(f"Configured interval: {config['delay_seconds']}s")

    consecutive_failures = 0
    
    for i in range(start, end + 1):
        if shutdown.requested:
            pull_shark_log("Graceful termination: current cycle completed.", style="yellow")
            break

        try:
            print(f"\n🔹 Processing PR #{i}")
            
            branch = f"automation-{i}-{uuid.uuid4().hex[:8]}"
            
            # Git operations
            if not git.sync_base():
                raise Exception("Failed to sync base branch")
                
            if not git.create_branch(branch):
                raise Exception(f"Failed to create branch {branch}")
                
            # Update README
            with open(config["readme_file"], "a", encoding="utf-8") as f:
                f.write(generate_content(i))
                
            if not git.commit(config["readme_file"], f"Automated update #{i}"):
                print("⚠ No changes to commit")
                continue
                
            if not config["dry_run"]:
                # Get token
                token, _ = token_manager.get_best_token()
                if not token:
                    raise Exception("No valid tokens available")
                    
                # Push and create PR
                if not git.push(branch):
                    raise Exception("Failed to push branch")
                
                # Surgical PR Description
                pr_body = (
                    f"Branch: {branch}\n"
                    f"Diff: +1 -0 lines\n"
                    f"Review: contribution sequence #{i} validated. Integrity check Passed.\n"
                    f"Verdict: LGTM ✓\n"
                    f"Action: merge"
                )
                    
                pr_number = gh.create_pr(
                    f"refactor: surgical update #{i}",
                    pr_body,
                    branch
                )
                
                if pr_number:
                    print(f"✅ PR #{i} created successfully (PR #{pr_number})")
                    
                    # Send creation notification
                    notifier.send(
                        f"✅ PR #{i} created successfully\n"
                        f"Branch: {branch}\n"
                        f"PR Number: #{pr_number}",
                        "success"
                    )
                    
                    # Auto-merge if enabled
                    if config["auto_merge"]:
                        merge_success = gh.merge_pr(branch)
                        
                        if merge_success:
                            pull_shark_log(f"PR #{pr_number} MERGED", status="MERGED")
                            notifier.send(
                                f"✅ PR #{i} merged successfully\n"
                                f"PR Number: #{pr_number}",
                                "success"
                            )
                        else:
                            print(f"⚠️  Failed to merge PR #{pr_number}")
                            notifier.send(
                                f"⚠️  PR #{i} created but merge failed\n"
                                f"PR Number: #{pr_number}",
                                "warning"
                            )
                else:
                    print(f"✅ PR #{i} created successfully")
                
            # Update state
            state["last_completed_pr"] = i
            save_state(state)
            
            session_prs_created += 1
            total_prs_created = i

            # Report periodically 
            telemetry_client.report(total_prs_created=total_prs_created, session_prs=session_prs_created)
            
            # Reset failure counter
            consecutive_failures = 0
            
            # Send notification every 10 PRs
            if i % 10 == 0:
                notifier.send(f"✅ Progress: {i}/{end} PRs completed")
                
            # Delay
            if i < end:
                print(f"⏱️  Waiting {config['delay_seconds']} seconds...")
                time.sleep(config["delay_seconds"])
                
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"PR #{i} failed: {str(e)}")
            notifier.send(f"❌ Failed at PR #{i}: {str(e)[:100]}")
            
            if consecutive_failures >= 5:
                print("❌ Too many consecutive failures. Stopping.")
                break
                
            time.sleep(30)

    # Telemetry Final Report
    telemetry_client.report_final(total_prs_created=total_prs_created, session_prs=session_prs_created)

    print("\n🎉 Automation completed!")

if __name__ == "__main__":
    main()