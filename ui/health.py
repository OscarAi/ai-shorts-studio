"""Health page: the same checks as `python cli.py doctor`."""

import streamlit as st

from studio import health, llm
from ui.core import repo


def page_health() -> None:
    st.header("🩺 Health check")
    checks = health.run_checks(repo)
    {health.OK: st.success, health.WARN: st.warning, health.ERROR: st.error}[
        max((c.level for c in checks), key=[health.OK, health.WARN, health.ERROR].index)
    ](health.summary(checks))
    for c in checks:
        st.markdown(f"{health.ICON[c.level]} **{c.name}**: {c.detail}"
                    + (f"  \n→ {c.fix}" if c.fix and c.level != health.OK else ""))
    st.caption("Same checks in the terminal: python cli.py doctor")

    st.subheader("AI services")
    st.caption("Sends one tiny request to each service that has a key in .env, and lists the "
               "models your key can use. (Terminal: python cli.py ai-test)")
    if st.button("Test connection", key="ai_test"):
        with st.spinner("Asking each AI service..."):
            for r in llm.test_connections(usage=repo):
                if not r.has_key:
                    st.markdown(f"– **{r.provider}**: {r.problem} · [get a free key]"
                                f"({r.signup_url})")
                elif r.ok:
                    st.markdown(f"✅ **{r.provider}**: answered with `{r.model}` in "
                                f"{r.seconds:g} s")
                else:
                    st.markdown(f"❌ **{r.provider}**: {r.problem}")
                if r.available:
                    st.caption(f"{r.provider} models for your key: " + ", ".join(r.available))
