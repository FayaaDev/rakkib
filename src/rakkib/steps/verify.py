"""Step 7 — Verify.

Run the final smoke tests for the deployed server.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

from rakkib.service_catalog import caddy_enabled, cloudflare_enabled
from rakkib.state import DEFAULT_STATE_FILE, State
from rakkib.steps import STEP_MODULES, VerificationResult

UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_verifications(state: State) -> list[VerificationResult]:
    """Import and run verify() from all other step modules.

    Reads from the cache written by cli._run_steps when available to avoid
    re-running each step's verify() three times per pull.
    """
    cache: dict[str, dict] = state.get("_step_verify_cache") or {}
    results: list[VerificationResult] = []

    for step_name, module_path in STEP_MODULES:
        if step_name == "caddy" and not caddy_enabled(state):
            results.append(VerificationResult.success("caddy", "skipped for internal exposure mode"))
            continue
        if step_name == "cloudflare" and not cloudflare_enabled(state):
            results.append(VerificationResult.success("cloudflare", "skipped for internal exposure mode"))
            continue
        if step_name in cache:
            cached = cache[step_name]
            results.append(VerificationResult(ok=cached["ok"], step=cached["step"], message=cached["message"]))
            continue

        try:
            module = __import__(module_path, fromlist=["verify"])
            verify_fn = getattr(module, "verify", None)
            if verify_fn is None:
                results.append(
                    VerificationResult.failure(
                        step_name,
                        f"Module {module_path} does not export verify()",
                    )
                )
                continue
            result = verify_fn(state)
            results.append(result)
        except ImportError:
            results.append(
                VerificationResult.failure(
                    step_name,
                    f"Step module {module_path} not found",
                )
            )
        except Exception as exc:
            results.append(
                VerificationResult.failure(
                    step_name,
                    f"verify() raised {type(exc).__name__}: {exc}",
                )
            )

    return results


def _verify_state_file_permissions(state: State) -> VerificationResult:
    state_path = state.path or Path(DEFAULT_STATE_FILE)
    if not state_path.exists():
        return VerificationResult.success("verify", f"{state_path} does not exist")

    mode = stat.S_IMODE(state_path.stat().st_mode)
    if mode & ~0o600:
        return VerificationResult.failure(
            "verify",
            f"{state_path} mode is {mode:#04o}; run `chmod 600 {state_path}` before continuing",
        )

    return VerificationResult.success("verify", f"{state_path} permissions are restricted")


def _verify_rendered_templates(state: State) -> VerificationResult:
    data_root = state.data_root
    docker_root = data_root / "docker"
    if not docker_root.exists():
        return VerificationResult.success("verify", f"{docker_root} does not exist")

    candidates = list(docker_root.rglob(".env"))
    candidates.extend(docker_root.rglob("docker-compose.yml"))
    candidates.extend((docker_root / "caddy").glob("Caddyfile*"))
    candidates.extend((docker_root / "caddy" / "routes").glob("*.caddy"))

    for path in sorted({candidate for candidate in candidates if candidate.is_file()}):
        text = path.read_text(encoding="utf-8", errors="replace")
        match = UNRESOLVED_PLACEHOLDER_RE.search(text)
        if match:
            key = match.group(1).strip()
            return VerificationResult.failure(
                "verify",
                f"{path} contains unresolved template placeholder `{key}`; rerun setup after providing the missing value",
            )

    return VerificationResult.success("verify", "rendered files contain no unresolved template placeholders")


def _print_summary(results: list[VerificationResult]) -> None:
    """Print a plain-text summary of verification results."""
    print("")
    print("=" * 60)
    print("CHECK SUMMARY")
    print("=" * 60)

    for r in results:
        status = "PASS" if r.ok else "FAIL"
        icon = "✓" if r.ok else "✗"
        print(f"{icon}  [{status}]  {r.step:<12}  {r.message}")
        if not r.ok and r.log_path:
            print(f"      Log: {r.log_path}")

    failures = [r for r in results if not r.ok]
    print("-" * 60)
    print(f"Total: {len(results)} checks, {len(failures)} failures")
    print("")

    if failures:
        print("ACTION REQUIRED")
        print("-" * 60)
        print("Some checks failed. Review the message above, then run `rakkib pull` again.")
        print("")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(state: State) -> None:
    results = [_verify_state_file_permissions(state), _verify_rendered_templates(state), *_collect_verifications(state)]
    failures = [r for r in results if not r.ok]

    if failures:
        _print_summary(results)
    else:
        print("Checks passed.")


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify(state: State) -> VerificationResult:
    results = [_verify_state_file_permissions(state), _verify_rendered_templates(state), *_collect_verifications(state)]
    failures = [r for r in results if not r.ok]

    if not failures:
        return VerificationResult.success("verify", "All sub-verifications passed")

    messages = [f"{r.step}: {r.message}" for r in failures]
    return VerificationResult.failure(
        "verify",
        f"{len(failures)} verification(s) failed: " + "; ".join(messages),
        state_slice={"failed_steps": [r.step for r in failures]},
    )
