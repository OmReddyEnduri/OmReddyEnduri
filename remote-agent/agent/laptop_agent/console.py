"""Headless approver: explicit typed answer on the terminal. No default, no timeout."""
import sys
import threading

from .engine import ApprovalRequest


class ConsoleApprover:
    def ask(self, req: ApprovalRequest, abort) -> str:
        print("\n" + "=" * 60 + "\nPERMISSION REQUIRED")
        print(f"Operation : {req.operation}\nArguments : {req.arguments}\nWorking dir: {req.cwd or '-'}")
        print(f"Files     : {req.files}\nReason    : {req.reason}\nImpact    : {req.impact}")
        if req.preview:
            print("--- preview ---\n" + req.preview[:1500])
        print("Type A (approve once), R (reject) or C (cancel):", flush=True)
        box: dict = {}

        def read():
            box["v"] = sys.stdin.readline()
        t = threading.Thread(target=read, daemon=True)
        t.start()
        while t.is_alive():
            if abort.is_set():
                return "cancel"
            t.join(0.2)
        a = (box.get("v") or "").strip().lower()
        return {"a": "approve", "r": "reject"}.get(a, "cancel")
