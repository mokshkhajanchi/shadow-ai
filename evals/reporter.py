"""Eval results reporter: formats and displays eval results."""

from collections import defaultdict


def print_report(results: list[dict]):
    """Print a formatted eval report."""
    print()
    print("=" * 60)
    print("  shadow.ai Evaluation Report")
    print("=" * 60)
    print()

    # Group by category
    categories = defaultdict(list)
    for r in results:
        categories[r.get("category", "unknown")].append(r)

    total_pass = 0
    total_fail = 0
    total_skip = 0
    critical_failures = []

    for category, cat_results in sorted(categories.items()):
        passed = sum(1 for r in cat_results if r.get("passed") is True)
        failed = sum(1 for r in cat_results if r.get("passed") is False)
        skipped = sum(1 for r in cat_results if r.get("skipped"))
        total = len(cat_results)
        total_pass += passed
        total_fail += failed
        total_skip += skipped

        pct = (passed / (passed + failed) * 100) if (passed + failed) > 0 else 0
        status = "✅" if failed == 0 else "❌"
        print(f"  {status} {category:<20s} {passed}/{passed + failed} passed ({pct:.0f}%)"
              + (f" [{skipped} skipped]" if skipped else ""))

        # Show failures
        for r in cat_results:
            if r.get("passed") is False:
                print(f"     ✗ {r['name']}")
                for check_name, check_result in r.get("checks", {}).items():
                    if not check_result["pass"]:
                        print(f"       - {check_result['detail']}")
                if r.get("critical_failed"):
                    critical_failures.append(r["name"])

    print()
    total_run = total_pass + total_fail
    overall_pct = (total_pass / total_run * 100) if total_run > 0 else 0
    print(f"  OVERALL: {total_pass}/{total_run} passed ({overall_pct:.1f}%)"
          + (f" [{total_skip} skipped]" if total_skip else ""))

    if critical_failures:
        print()
        print("  🔴 CRITICAL FAILURES:")
        for name in critical_failures:
            print(f"     - {name}")

    print()
    print("=" * 60)
