import os

# TestClient sends `Host: testserver`. It goes in here rather than in the
# defaults, which describe a real deployment; set before `app` is imported,
# because the middleware reads the list once, at startup.
os.environ["SMOLPLAN_ALLOWED_HOSTS"] = "localhost,127.0.0.1,[::1],testserver"
