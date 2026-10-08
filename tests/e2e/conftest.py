"""Print the app log tail when an e2e test fails (makes CI failures debuggable)."""
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" and rep.failed:
        w = item.funcargs.get("w")
        if w is not None:
            try:
                rep.sections.append(("mr-pilot log (tail)", "\n".join(w.app.log().splitlines()[-60:])))
            except Exception:
                pass
            try:
                rep.sections.append(("telegram (last 8)", "\n---\n".join(t[:400] for t in w.tg.texts()[-8:])))
                rep.sections.append(("telegram answers", repr(w.tg.answers[-5:])))
            except Exception:
                pass
