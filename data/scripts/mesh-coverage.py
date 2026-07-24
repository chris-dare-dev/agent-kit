#!/usr/bin/env python3
"""mesh-coverage.py — pod-level Istio sidecar coverage audit (native-sidecar-aware).

Answers "which workloads are ACTUALLY in the mesh?" by inspecting every pod and
checking for the `istio-proxy` proxy in BOTH `.spec.initContainers` AND
`.spec.containers`.

WHY BOTH: on Istio >=1.24 / k8s >=1.29 the platform injects the Envoy proxy as a
Kubernetes NATIVE SIDECAR — `istio-proxy` lands in `.spec.initContainers` with
`restartPolicy: Always` (it stays `running`), NOT in `.spec.containers`. Any audit
that counts only `.spec.containers[*].name == "istio-proxy"` false-negatives every
injected workload and catches only gateway pods (a gateway's proxy IS a regular
container) — the exact artifact behind the 2026-06-23 "prod has no sidecars" alarm.
See memory `reference-istio-native-sidecars-prod`.

Read-only: only `kubectl get`. No mutations.

Usage:
    mesh-coverage.py <context> [<context> ...]
    mesh-coverage.py --json <context> ...        # machine-readable per-pod output

Example:
    mesh-coverage.py core-services-prod operations-prod observability-prod \\
                     tenant-demo-prod tenant-ngs-prod tenant-workspace-prod
"""
import json
import subprocess
import sys

# Namespaces that are intentionally OUTSIDE the mesh (control-plane / pre-mesh
# components, hostNetwork agents, or things that must function before istiod).
# A running workload here with no proxy is EXPECTED, not a gap.
EXPECTED_UNINJECTED = {
    "kube-system", "istio-system", "cert-manager", "crossplane-system",
    "external-secrets", "external-dns", "velero", "blackbox", "platform-probes",
    "pushgateway", "kube-node-lease", "kube-public", "amazon-cloudwatch",
    "karpenter", "kyverno",
}
PROXY = "istio-proxy"


def kc(ctx, args):
    try:
        out = subprocess.run(
            ["kubectl", "--context", ctx, *args],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return None, str(e)
    if out.returncode != 0:
        return None, out.stderr.strip()
    return out.stdout, None


def classify(pod):
    """Return one of: native | classic | gateway | none.

    native  = proxy is a native-sidecar init container (the modern default)
    classic = proxy is a regular container but pod is a workload (legacy injection)
    gateway = proxy is the ONLY app container (the pod IS a gateway, not injected)
    none    = no proxy at all
    """
    spec = pod.get("spec", {})
    inits = [c.get("name") for c in spec.get("initContainers", []) or []]
    conts = [c.get("name") for c in spec.get("containers", []) or []]
    if PROXY in inits:
        return "native"
    if PROXY in conts:
        # A standalone gateway runs istio-proxy as its sole/primary container.
        return "gateway" if conts == [PROXY] else "classic"
    return "none"


def main(argv):
    as_json = False
    if argv and argv[0] == "--json":
        as_json = True
        argv = argv[1:]
    if not argv:
        print(__doc__)
        return 2

    all_rows = []
    grand = {"native": 0, "classic": 0, "gateway": 0, "none": 0}
    for ctx in argv:
        out, err = kc(ctx, ["get", "pods", "-A", "-o", "json"])
        if out is None:
            print(f"\n## {ctx}\n  UNREACHABLE: {err}", file=sys.stderr)
            continue
        try:
            items = json.loads(out).get("items", [])
        except json.JSONDecodeError:
            print(f"\n## {ctx}\n  parse error", file=sys.stderr)
            continue

        per_ns = {}   # ns -> {native, classic, gateway, none}
        ctot = {"native": 0, "classic": 0, "gateway": 0, "none": 0}
        for p in items:
            ns = p["metadata"]["namespace"]
            kind = classify(p)
            d = per_ns.setdefault(ns, {"native": 0, "classic": 0, "gateway": 0, "none": 0})
            d[kind] += 1
            ctot[kind] += 1
            if as_json:
                all_rows.append({"ctx": ctx, "ns": ns,
                                 "pod": p["metadata"]["name"], "mesh": kind})
        for k in grand:
            grand[k] += ctot[k]

        if not as_json:
            enrolled = ctot["native"] + ctot["classic"]
            print(f"\n## {ctx}  —  {enrolled} mesh-enrolled workloads "
                  f"(native={ctot['native']} classic={ctot['classic']}), "
                  f"{ctot['gateway']} gateway, {ctot['none']} un-injected")
            hdr = "{:32} {:>7} {:>7} {:>7} {:>7}".format(
                "NAMESPACE", "native", "classic", "gw", "none")
            print(hdr)
            warns = []
            for ns in sorted(per_ns):
                d = per_ns[ns]
                inj = d["native"] + d["classic"]
                flag = ""
                if inj:
                    flag = " <- INJECTED"
                elif d["gateway"]:
                    flag = " <- gateway"
                elif d["none"] and ns not in EXPECTED_UNINJECTED:
                    flag = " <- *** UN-INJECTED workload ns ***"
                    warns.append(ns)
                print("{:32} {:>7} {:>7} {:>7} {:>7}{}".format(
                    ns, d["native"], d["classic"], d["gateway"], d["none"], flag))
            if warns:
                print(f"  WARN: {len(warns)} workload ns with ZERO mesh enrollment "
                      f"(not in expected-uninjected set): {', '.join(warns)}")

    if as_json:
        print(json.dumps(all_rows, indent=2))
    else:
        enrolled = grand["native"] + grand["classic"]
        print(f"\n=== FLEET TOTAL: {enrolled} mesh-enrolled "
              f"(native={grand['native']} classic={grand['classic']}), "
              f"{grand['gateway']} gateway, {grand['none']} un-injected ===")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
