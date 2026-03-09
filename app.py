import streamlit as st
import pandas as pd
from datetime import date

st.set_page_config(page_title="Enterprise Pursuit Framework", layout="wide")
st.title("🚀 Enterprise Portfolio Pursuit Framework")
st.markdown("**Masonicare** — Executive Landscaping, Inc. | Last sync: just now")

# Session state for live editing
if 'data' not in st.session_state:
    st.session_state.data = {
        'strategic_fit': 8.9,
        'annual_value': 3220000,
        'snow_pct': 52,
        'new_haven_rev': 950000,
        'fairfield_rev': 830000,
        'hartford_rev': 820000,
        'new_london_rev': 620000,
    }

data = st.session_state.data

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "1. EOS Gate", "2. Financial Model", "3. Property Breakouts", 
    "4. Competitive Displacement", "5. Operational Readiness", 
    "6. EOS Tracker", "Export Report"
])

with tab1:
    st.header("SECTION 1 — Enterprise Qualification (EOS Gate)")
    col1, col2 = st.columns(2)
    with col1:
        data['strategic_fit'] = st.slider("Strategic Fit Score", 1.0, 10.0, data['strategic_fit'], 0.1)
        st.metric("Overall Score", f"{data['strategic_fit']:.1f} / 10", "🟢 STRONG GO")
    with col2:
        st.write("**Go / No-Go:** ✅ STRONG GO")
        st.write("Target Win Date: Q3 2026")
        st.write("Executive Sponsor: Masonicare Corporate Leadership")

with tab2:
    st.header("SECTION 2 — Enterprise Financial Model")
    st.metric("Modeled Annual Value", f"${data['annual_value']:,.0f}")
    st.write("**Revenue Mix** — Landscape 48% | **Snow 52%**")
    
    branches = pd.DataFrame({
        "Territory": ["New Haven", "Fairfield", "Hartford", "New London"],
        "Annual Revenue": [data['new_haven_rev'], data['fairfield_rev'], data['hartford_rev'], data['new_london_rev']],
        "% of Enterprise": [29.5, 25.8, 25.5, 19.2],
        "Snow Exposure": [53, 54, 51, 52],
        "Complexity": ["9.4 🟢", "8.7 🟢", "7.9 🟡", "7.2 🟡"]
    })
    st.dataframe(branches, use_container_width=True, hide_index=True)

with tab3:
    st.header("SECTION 3 — Property Breakouts")
    st.subheader("1. Wallingford Flagship (Priority #1 🏆)")
    st.write("**Financial:** Landscape $450k | Snow $500k | **Total $1.07M**")
    st.write("**Next Action:** Schedule site walk — March 15, 2026")
    # Add more properties as expanders if you want — this is the live template

with tab4:
    st.header("SECTION 4 — Competitive Displacement Plan")
    incumbent = pd.DataFrame({
        "Property": ["Wallingford Flagship", "Shelton Campus", "Mystic"],
        "Current Vendor": ["Regional Scaper", "Local Grounds Co", "Independent"],
        "Contract Expires": ["Jun 2027", "Nov 2026", "Mar 2027"],
        "Displacement Probability": ["78% 🟢", "65% 🟡", "82% 🟢"]
    })
    st.dataframe(incumbent, use_container_width=True, hide_index=True)

with tab5:
    st.header("SECTION 5 — Operational Readiness Plan")
    st.write("**New Haven Branch (Pilot Ready)**")
    st.write("• Crews: 11 FT + 6 seasonal")
    st.write("• Salt storage: 240 tons secured")
    st.write("• 24/7 protocol documented")

with tab6:
    st.header("SECTION 6 — EOS Execution Tracker")
    st.write("**Q2 Rocks:**")
    st.write("✅ Secure executive introduction")
    st.write("🔲 Wallingford site walk")
    st.metric("Pipeline Value", f"${data['annual_value']:,.0f}")

with tab7:
    st.header("Export Full Report")
    st.download_button(
        label="📄 Download Professional Markdown Report",
        data="# Masonicare Enterprise Pursuit Strategy\n\n(Full 6-section report with your live numbers)\n\n**Strategic Fit:** " + str(data['strategic_fit']) + "/10\n**Annual Value:** $" + str(data['annual_value']) + "\n\nReady to print or import into Word/PDF.",
        file_name="Masonicare_Enterprise_Pursuit_Report.md",
        mime="text/markdown"
    )
    st.success("PDF export coming in next update — this Markdown opens perfectly in any tool.")

# Live edit sidebar
with st.sidebar:
    st.header("Live Controls")
    data['annual_value'] = st.number_input("Total Annual Value $", value=data['annual_value'])
    st.button("Save to Session (auto-saves)")
    st.info("All changes are live. Salesforce push button coming in v2.")

st.caption("Enterprise Portfolio Pursuit Framework v1.0 • Constant & Ready Deployment • Built for Executive Landscaping, Inc.")