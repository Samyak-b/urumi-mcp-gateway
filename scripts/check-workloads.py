"""Check every workload in the project namespaces against the assignment's rules, from the live cluster.

    kubectl get deploy,statefulset,job -A -o json | python3 scripts/check-workloads.py store tools platform

Rules: every long-running container (Deployments, StatefulSets) has a readiness probe, a liveness probe, resource
requests and resource limits. Jobs run to completion, so they need resources but no probes. It also reports who
each container runs as and whether it can reach the Kubernetes API, for the security part of the demo.
Exit code 1 if any rule is broken.
"""

import json
import sys


def main() -> int:
    namespaces = set(sys.argv[1:])
    items = [it for it in json.load(sys.stdin)["items"] if not namespaces or it["metadata"]["namespace"] in namespaces]
    failures = 0
    print(f"{'namespace/workload':34} {'container':12} {'ready':6} {'live':6} {'requests':9} {'limits':7} {'runAsNonRoot':13} api-token")
    for it in items:
        kind = it["kind"]
        spec = it["spec"]["template"]["spec"]
        pod_sc = spec.get("securityContext") or {}
        token = spec.get("automountServiceAccountToken", True)
        for c in spec.get("initContainers", []) + spec["containers"]:
            res = c.get("resources") or {}
            sc = c.get("securityContext") or {}
            checks = {
                "ready": "readinessProbe" in c or kind == "Job",
                "live": "livenessProbe" in c or kind == "Job",
                "requests": bool(res.get("requests")),
                "limits": bool(res.get("limits")),
            }
            nonroot = sc.get("runAsNonRoot", pod_sc.get("runAsNonRoot"))
            mark = lambda ok: "yes" if ok else "NO"  # noqa: E731
            name = f"{it['metadata']['namespace']}/{it['metadata']['name']}"
            probe = lambda ok: "n/a" if kind == "Job" else mark(ok)  # noqa: E731
            if kind == "Job":
                name += " (job)"
            print(f"{name:34} {c['name']:12} {probe(checks['ready']):6} {probe(checks['live']):6} "
                  f"{mark(checks['requests']):9} {mark(checks['limits']):7} {str(bool(nonroot)):13} {'mounted' if token else 'none'}")
            failures += sum(not ok for ok in checks.values())
    print(f"\n{len(items)} workloads checked: " + ("all have probes, requests and limits" if not failures else f"{failures} problems"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
