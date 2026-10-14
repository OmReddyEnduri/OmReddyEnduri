import os, sys, tempfile
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["LAPTOP_AGENT_HOME"] = tempfile.mkdtemp(prefix="agenthome-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "relay"))
