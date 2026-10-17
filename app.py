"""
PRM Cluster Manager
Streamlit app backed by a Google Sheet:
  - sheet 'prms'          : prm_name | state   (row 1 = header)
  - sheets 'group_<State>' : prm_name | group
The 'state' value in 'prms' must match the sheet name without the prefix,
e.g. state 'Ekiti 1' <-> sheet 'group_Ekiti 1' (case and extra spaces ignored).
"""

import time

import gspread
import pandas as pd
import streamlit as st
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="PRM Cluster Management", page_icon="🗂️", layout="wide")

PRM_SHEET = "prms"
GROUP_PREFIX = "group_"
PRM_NAME_COL = 1   # column A in 'prms'
PRM_STATE_COL = 2  # column B in 'prms'
PRMS_TTL = 60 * 60  # seconds; how long a loaded prms list is reused

ss = st.session_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def clean(s) -> str:
    """Collapse whitespace (fixes leading/trailing/double spaces in the sheet)."""
    return " ".join(str(s).split())


def norm(s) -> str:
    """Comparison key: cleaned + upper case."""
    return clean(s).upper()


def label(sheet_title: str) -> str:
    """'group_Ekiti 1' -> 'Ekiti 1'."""
    return sheet_title[len(GROUP_PREFIX):] if sheet_title.startswith(GROUP_PREFIX) else sheet_title


# ---------------------------------------------------------------------------
# Google Sheets access
# ---------------------------------------------------------------------------
conn = st.connection("gsheets", type=GSheetsConnection)

# Google allows 60 read requests per minute per user. conn.read() costs ~3
# requests per worksheet (open spreadsheet, find worksheet, fetch values), so
# reading 11 sheets one by one blows the limit. Instead the app reuses the
# spreadsheet opened by the connection and fetches the group_ sheets in ONE
# request.
#
# Caching:
#   - group_ sheets: only this app changes them, so no timer. Refetched when a
#     user saves (before the write, to validate live, and after), removes a
#     PRM, or clicks Reload.
#   - prms: its own read, reused for PRMS_TTL, then refetched on next use.
#     Reload refetches it immediately.
#   - tab list: refetched only on Reload.
#   first load : 4 reads   save / remove : 2 reads + 1 write
#   prms       : at most 1 read per PRMS_TTL while the app is in use


def with_retry(fn, *args, **kwargs):
    """Retry on 429 (rate limit) with backoff instead of failing the page."""
    for attempt in range(4):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if getattr(e.response, "status_code", None) != 429 or attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


@st.cache_resource(show_spinner="Connecting to Google Sheets...")
def get_spreadsheet():
    # GSheetsConnection has no public batch-read or list-worksheets call, so use
    # the gspread Spreadsheet it opens for us (service-account mode).
    return with_retry(conn.client._open_spreadsheet)


@st.cache_resource(show_spinner=False)
def worksheet_map() -> dict:
    """title -> gspread Worksheet (1 metadata request; cleared by Reload only)."""
    return {ws.title: ws for ws in with_retry(get_spreadsheet().worksheets)}


@st.cache_data(show_spinner="Loading sheets...")
def load_all() -> dict[str, list[list[str]]]:
    """group_ sheets only, one batched request. Changed only by this app,
    so cached until a save, removal or Reload."""
    titles = [t for t in worksheet_map() if t != PRM_SHEET]
    ranges = ["'" + t.replace("'", "''") + "'" for t in titles]
    resp = with_retry(get_spreadsheet().values_batch_get, ranges)
    return {t: vr.get("values", []) for t, vr in zip(titles, resp.get("valueRanges", []))}


def live_rows(sheet: str) -> list[list[str]]:
    """Fresh read of one worksheet (1 request), bypassing the cache."""
    return with_retry(worksheet_map()[sheet].get_all_values)


@st.cache_data(ttl=PRMS_TTL, show_spinner="Loading PRM list...")
def load_prms() -> dict[str, list[str]]:
    """Own read (1 request). Refetched after PRMS_TTL on next use, or on Reload."""
    rows = with_retry(worksheet_map()[PRM_SHEET].get_all_values)
    out: dict[str, set[str]] = {}
    for r in rows[1:]:
        r = r + [""] * max(PRM_NAME_COL, PRM_STATE_COL)
        name, state = clean(r[PRM_NAME_COL - 1]), clean(r[PRM_STATE_COL - 1])
        if name and state:
            out.setdefault(norm(state), set()).add(name)
    return {k: sorted(v) for k, v in out.items()}


def prms_for(sheet: str) -> list[str]:
    """PRMs listed in 'prms' for the state behind this group sheet."""
    return load_prms().get(norm(label(sheet)), [])


def to_groups_df(rows: list[list[str]]) -> pd.DataFrame:
    data = []
    for r in rows[1:]:
        r = (r + ["", ""])[:2]
        if clean(r[0]):
            data.append({"prm_name": clean(r[0]), "group": clean(r[1])})
    return pd.DataFrame(data, columns=["prm_name", "group"])


@st.cache_data(show_spinner=False)
def load_groups() -> dict[str, pd.DataFrame]:
    """state sheet title -> DataFrame(prm_name, group)."""
    return {t: to_groups_df(rows) for t, rows in load_all().items() if t != PRM_SHEET}


def refresh():
    """Before/after a write: group_ sheets only, prms untouched."""
    load_all.clear()
    load_groups.clear()


def refresh_all():
    """Reload button: group_ sheets, prms and the tab list."""
    worksheet_map.clear()
    load_prms.clear()
    refresh()


def already_assigned_errors(prms: list[str], groups, sheet: str) -> list[str]:
    """Duplicate check within the chosen state's sheet only."""
    df = groups.get(sheet, pd.DataFrame(columns=["prm_name", "group"]))
    hits: dict[str, list[str]] = {}
    for name, grp in zip(df["prm_name"], df["group"]):
        hits.setdefault(norm(name), []).append(grp)
    return [
        f"**{p}** already belongs to cluster **{grp}** in {label(sheet)}."
        for p in prms
        for grp in hits.get(norm(p), [])
    ]


def clusters_in(groups, sheet) -> list[str]:
    df = groups.get(sheet, pd.DataFrame(columns=["group"]))
    return sorted({g for g in df["group"] if g}, key=str.upper)


def delete_row_numbers(sheet: str, row_numbers: list[int]):
    """Delete sheet rows (1-based) in ONE write request, whatever the count.
    Contiguous rows are grouped; ranges go bottom-up so indexes stay valid."""
    ranges = []
    for n in sorted(set(row_numbers), reverse=True):
        if ranges and ranges[-1][0] == n + 1:
            ranges[-1][0] = n          # extend current block upwards
        else:
            ranges.append([n, n])      # [first, last]
    ws = worksheet_map()[sheet]
    body = {"requests": [
        {"deleteDimension": {"range": {
            "sheetId": ws.id, "dimension": "ROWS",
            "startIndex": first - 1, "endIndex": last,
        }}}
        for first, last in ranges
    ]}
    with_retry(get_spreadsheet().batch_update, body)


def append_rows(sheet: str, rows: list[list[str]]):
    """Add rows below the existing data (1 write request)."""
    with_retry(
        worksheet_map()[sheet].append_rows,
        rows, value_input_option="RAW", table_range="A1",
    )


# ---------------------------------------------------------------------------
# Session state
#
# Review/confirm screens work from a snapshot ("<tab>_pending") taken when the
# user clicks Review. Input widgets are hidden during review, and Edit/Cancel
# writes the snapshot back into the widget keys so nothing the user typed is lost.
# ---------------------------------------------------------------------------
for p in ("t1", "t2", "t3", "t4"):
    ss.setdefault(f"{p}_stage", "edit")
    ss.setdefault(f"{p}_errors", [])
    ss.setdefault(f"{p}_success", "")
    ss.setdefault(f"{p}_pending", None)


def back_to_edit(prefix, errors=None):
    """Return to the input form, restoring the user's inputs from the snapshot."""
    pending = ss[f"{prefix}_pending"] or {}
    for field, value in pending.items():
        ss[f"{prefix}_{field}"] = value
    ss[f"{prefix}_stage"] = "edit"
    ss[f"{prefix}_errors"] = errors or []


def on_state_change(*keys):
    """Clear selections that belonged to the previously chosen state."""
    for k in keys:
        ss[k] = [] if k.endswith("_prms") else None


def no_prms_warning(sheet):
    st.warning(
        f"No PRMs found for **{label(sheet)}** in the '{PRM_SHEET}' sheet. "
        "Check that its state column matches this state name."
    )


def show_messages(prefix):
    for e in ss[f"{prefix}_errors"]:
        st.error(e)
    if ss[f"{prefix}_success"]:
        st.success(ss[f"{prefix}_success"])
        ss[f"{prefix}_success"] = ""


# ---------------------------------------------------------------------------
# Tab 1: new cluster
# ---------------------------------------------------------------------------
def t1_validate(groups, state, cluster, prms) -> list[str]:
    errs = []
    if not cluster:
        errs.append("Enter a cluster name.")
    elif norm(cluster) in {norm(c) for c in clusters_in(groups, state)}:
        errs.append(
            f"Cluster **{cluster}** already exists in {label(state)}. "
            "Use the 'Add PRM to cluster' tab instead."
        )
    if not prms:
        errs.append("Add at least one PRM.")
    errs += already_assigned_errors(prms, groups, state)
    return errs


def t1_review():
    pending = {
        "state": ss.t1_state,
        "cluster": clean(ss.t1_cluster),
        "prms": list(ss.t1_prms),
    }
    ss.t1_pending = pending
    errs = t1_validate(load_groups(), **pending)
    ss.t1_errors = errs
    ss.t1_stage = "edit" if errs else "review"


def t1_submit():
    pending = ss.t1_pending
    refresh()  # re-check against live data before writing
    errs = t1_validate(load_groups(), **pending)
    if errs:
        back_to_edit("t1", errs)
        return
    try:
        append_rows(pending["state"], [[p, pending["cluster"]] for p in pending["prms"]])
    except Exception as e:  # noqa: BLE001
        back_to_edit("t1", [f"Write failed: {e}"])
        return
    refresh()
    ss.t1_success = (
        f"Cluster **{pending['cluster']}** created in {label(pending['state'])} "
        f"with {len(pending['prms'])} PRM(s)."
    )
    ss.t1_state = pending["state"]
    ss.t1_cluster, ss.t1_prms = "", []
    ss.t1_pending, ss.t1_stage = None, "edit"


# ---------------------------------------------------------------------------
# Tab 2: add PRM to existing cluster
# ---------------------------------------------------------------------------
def t2_validate(groups, state, cluster, prms) -> list[str]:
    errs = []
    if not cluster:
        errs.append("Choose a cluster.")
    if not prms:
        errs.append("Add at least one PRM.")
    errs += already_assigned_errors(prms, groups, state)
    return errs


def t2_review():
    pending = {
        "state": ss.t2_state,
        "cluster": ss.get("t2_cluster"),
        "prms": list(ss.t2_prms),
    }
    ss.t2_pending = pending
    errs = t2_validate(load_groups(), **pending)
    ss.t2_errors = errs
    ss.t2_stage = "edit" if errs else "review"


def t2_submit():
    pending = ss.t2_pending
    refresh()
    errs = t2_validate(load_groups(), **pending)
    if errs:
        back_to_edit("t2", errs)
        return
    try:
        append_rows(pending["state"], [[p, pending["cluster"]] for p in pending["prms"]])
    except Exception as e:  # noqa: BLE001
        back_to_edit("t2", [f"Write failed: {e}"])
        return
    refresh()
    ss.t2_success = (
        f"Added {len(pending['prms'])} PRM(s) to **{pending['cluster']}** "
        f"({label(pending['state'])})."
    )
    ss.t2_state, ss.t2_cluster = pending["state"], pending["cluster"]
    ss.t2_prms = []
    ss.t2_pending, ss.t2_stage = None, "edit"


# ---------------------------------------------------------------------------
# Tab 3: remove PRM from cluster
# ---------------------------------------------------------------------------
def t3_review():
    ss.t3_pending = {"state": ss.t3_state, "cluster": ss.t3_cluster, "prm": ss.t3_prm}
    ss.t3_errors = []
    ss.t3_stage = "review"


def t3_remove():
    pending = ss.t3_pending
    sheet, cluster, prm = pending["state"], pending["cluster"], pending["prm"]
    try:
        ws = worksheet_map()[sheet]
        rows = live_rows(sheet)  # fresh read so row numbers are current
        targets = [
            i
            for i, r in enumerate(rows[1:], start=2)
            if len(r) >= 2 and norm(r[0]) == norm(prm) and norm(r[1]) == norm(cluster)
        ]
        if not targets:
            refresh()
            back_to_edit("t3", [f"{prm} was not found in {cluster}. It may already have been removed."])
            ss.t3_prm = None
            return
        for i in sorted(targets, reverse=True):  # bottom-up so row numbers stay valid
            with_retry(ws.delete_rows, i)
    except Exception as e:  # noqa: BLE001
        back_to_edit("t3", [f"Delete failed: {e}"])
        return
    refresh()
    ss.t3_success = f"Removed **{prm}** from **{cluster}** ({label(sheet)})."
    ss.t3_state, ss.t3_cluster, ss.t3_prm = sheet, cluster, None
    ss.t3_pending, ss.t3_stage, ss.t3_errors = None, "edit", []


# ---------------------------------------------------------------------------
# Tab 4: rename or delete a whole cluster
# ---------------------------------------------------------------------------
def cluster_rows(rows: list[list[str]], cluster: str) -> list[int]:
    """1-based sheet row numbers whose group matches cluster (header skipped)."""
    return [
        i for i, r in enumerate(rows[1:], start=2)
        if len(r) >= 2 and clean(r[0]) and norm(r[1]) == norm(cluster)
    ]


def t4_validate(groups, state, cluster, action, new_name) -> list[str]:
    errs = []
    if not cluster:
        errs.append("Choose a cluster.")
    if action == "Rename" and cluster:
        if not new_name:
            errs.append("Enter the new cluster name.")
        elif new_name == cluster:
            errs.append("The new name is the same as the current name.")
        elif norm(new_name) != norm(cluster) and norm(new_name) in {
            norm(c) for c in clusters_in(groups, state)
        }:
            errs.append(
                f"Cluster **{new_name}** already exists in {label(state)}. "
                "Choose a different name."
            )
    return errs


def t4_review():
    pending = {
        "state": ss.t4_state,
        "cluster": ss.get("t4_cluster"),
        "action": ss.t4_action,
        "new_name": clean(ss.get("t4_new_name", "")),
    }
    ss.t4_pending = pending
    errs = t4_validate(load_groups(), **pending)
    ss.t4_errors = errs
    ss.t4_stage = "edit" if errs else "review"


def t4_submit():
    pending = ss.t4_pending
    sheet, cluster, action, new_name = (
        pending["state"], pending["cluster"], pending["action"], pending["new_name"]
    )
    try:
        refresh()  # validate against live data before writing
        errs = t4_validate(load_groups(), **pending)
        if errs:
            back_to_edit("t4", errs)
            return
        rows = live_rows(sheet)  # fresh read so row numbers are current
        targets = cluster_rows(rows, cluster)
        if not targets:
            back_to_edit("t4", [f"Cluster **{cluster}** was not found. It may already have been changed."])
            ss.t4_cluster = None
            return
        if action == "Rename":
            updates = [{"range": f"B{i}", "values": [[new_name]]} for i in targets]
            with_retry(worksheet_map()[sheet].batch_update, updates, value_input_option="RAW")
        else:
            delete_row_numbers(sheet, targets)
    except Exception as e:  # noqa: BLE001
        back_to_edit("t4", [f"{action} failed: {e}"])
        return
    refresh()
    if action == "Rename":
        ss.t4_success = (
            f"Renamed **{cluster}** to **{new_name}** in {label(sheet)} "
            f"({len(targets)} PRM(s) updated)."
        )
    else:
        ss.t4_success = (
            f"Deleted cluster **{cluster}** from {label(sheet)} "
            f"({len(targets)} PRM(s) removed)."
        )
    ss.t4_state, ss.t4_action = sheet, action
    ss.t4_cluster, ss.t4_new_name = None, ""
    ss.t4_pending, ss.t4_stage, ss.t4_errors = None, "edit", []


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("PRM Cluster Management")

try:
    groups = load_groups()
    prm_map = load_prms()
except Exception as e:  # noqa: BLE001
    st.error(f"Could not connect to the Google Sheet: {e}")
    st.stop()

states = sorted(groups.keys(), key=lambda s: label(s).upper())
if not states:
    st.warning("No state sheets found.")
    st.stop()

with st.sidebar:
    st.caption(f"{sum(map(len, prm_map.values()))} PRMs · {len(states)} states")
    if st.button("Reload data from sheet"):
        refresh_all()
        st.rerun()

tab1, tab2, tab3, tab4 = st.tabs(
    ["New cluster", "Add PRM to cluster", "Remove PRM from cluster", "Manage cluster"]
)

# ---- Tab 1 ----------------------------------------------------------------
with tab1:
    st.subheader("Create a new cluster")
    if ss.t1_stage == "edit":
        state1 = st.selectbox(
            "State", states, key="t1_state", format_func=label,
            on_change=on_state_change, args=("t1_prms",),
        )
        st.text_input("Cluster name", key="t1_cluster")
        options1 = prms_for(state1)
        if not options1:
            no_prms_warning(state1)
        st.multiselect(
            "PRMs in this cluster",
            options1,
            key="t1_prms",
            placeholder="Type to search, e.g. Adebayo",
        )
        show_messages("t1")
        st.button("Review", key="t1_review_btn", on_click=t1_review, type="primary")
    else:
        pend = ss.t1_pending
        st.info(
            f"Please confirm: creating **{pend['cluster']}** in "
            f"**{label(pend['state'])}** with these {len(pend['prms'])} PRM(s)."
        )
        st.dataframe(
            pd.DataFrame({"prm_name": pend["prms"], "group": pend["cluster"]}),
            hide_index=True,
            width="stretch",
        )
        c1, c2, _ = st.columns([1, 1, 4])
        c1.button("Confirm and submit", key="t1_go", on_click=t1_submit, type="primary")
        c2.button("Edit", key="t1_edit", on_click=back_to_edit, args=("t1",))

# ---- Tab 2 ----------------------------------------------------------------
with tab2:
    st.subheader("Add PRM(s) to an existing cluster")
    if ss.t2_stage == "edit":
        state2 = st.selectbox(
            "State", states, key="t2_state", format_func=label,
            on_change=on_state_change, args=("t2_cluster", "t2_prms"),
        )
        st.selectbox(
            "Cluster",
            clusters_in(groups, state2),
            key="t2_cluster",
            index=None,
            placeholder="Choose a cluster",
        )
        options2 = prms_for(state2)
        if not options2:
            no_prms_warning(state2)
        st.multiselect(
            "PRM(s) to add",
            options2,
            key="t2_prms",
            placeholder="Type to search, e.g. Adebayo",
        )
        show_messages("t2")
        st.button("Review", key="t2_review_btn", on_click=t2_review, type="primary")
    else:
        pend = ss.t2_pending
        st.info(
            f"Please confirm: adding {len(pend['prms'])} PRM(s) to "
            f"**{pend['cluster']}** in **{label(pend['state'])}**."
        )
        st.dataframe(
            pd.DataFrame({"prm_name": pend["prms"], "group": pend["cluster"]}),
            hide_index=True,
            width="stretch",
        )
        c1, c2, _ = st.columns([1, 1, 4])
        c1.button("Confirm and submit", key="t2_go", on_click=t2_submit, type="primary")
        c2.button("Edit", key="t2_edit", on_click=back_to_edit, args=("t2",))

# ---- Tab 3 ----------------------------------------------------------------
with tab3:
    st.subheader("Remove a PRM from a cluster")
    if ss.t3_stage == "edit":
        state3 = st.selectbox(
            "State", states, key="t3_state", format_func=label,
            on_change=on_state_change, args=("t3_cluster", "t3_prm"),
        )
        cluster3 = st.selectbox(
            "Cluster",
            clusters_in(groups, state3),
            key="t3_cluster",
            index=None,
            placeholder="Choose a cluster",
        )
        members = []
        if cluster3:
            df = groups[state3]
            members = sorted(df.loc[df["group"].map(norm) == norm(cluster3), "prm_name"].unique())
            st.caption(f"{len(members)} PRM(s) in this cluster")
        st.selectbox(
            "PRM",
            members,
            key="t3_prm",
            index=None,
            placeholder="Type to search members of this cluster",
            disabled=not cluster3,
        )
        show_messages("t3")
        st.button(
            "Remove",
            key="t3_review_btn",
            on_click=t3_review,
            disabled=not ss.get("t3_prm"),
        )
    else:
        pend = ss.t3_pending
        st.warning(
            f"Remove **{pend['prm']}** from **{pend['cluster']}** ({label(pend['state'])})?"
        )
        c1, c2, _ = st.columns([1, 1, 4])
        c1.button("Yes, remove", key="t3_go", on_click=t3_remove, type="primary")
        c2.button("Cancel", key="t3_cancel", on_click=back_to_edit, args=("t3",))

# ---- Tab 4 ----------------------------------------------------------------
with tab4:
    st.subheader("Rename or delete a cluster")
    if ss.t4_stage == "edit":
        state4 = st.selectbox(
            "State", states, key="t4_state", format_func=label,
            on_change=on_state_change, args=("t4_cluster",),
        )
        cluster4 = st.selectbox(
            "Cluster",
            clusters_in(groups, state4),
            key="t4_cluster",
            index=None,
            placeholder="Choose a cluster",
        )
        members4 = []
        if cluster4:
            df = groups[state4]
            members4 = sorted(df.loc[df["group"].map(norm) == norm(cluster4), "prm_name"])
            st.caption(f"{len(members4)} PRM(s) in this cluster")
        action4 = st.radio(
            "Action", ["Rename", "Delete"], key="t4_action", horizontal=True,
        )
        if action4 == "Rename":
            st.text_input("New cluster name", key="t4_new_name", disabled=not cluster4)
        show_messages("t4")
        st.button(
            "Review", key="t4_review_btn", on_click=t4_review, type="primary",
            disabled=not cluster4,
        )
    else:
        pend = ss.t4_pending
        df = groups.get(pend["state"], pd.DataFrame(columns=["prm_name", "group"]))
        members4 = sorted(df.loc[df["group"].map(norm) == norm(pend["cluster"]), "prm_name"])
        if pend["action"] == "Rename":
            st.info(
                f"Please confirm: rename **{pend['cluster']}** to **{pend['new_name']}** "
                f"in **{label(pend['state'])}**. These {len(members4)} PRM(s) move with it."
            )
            confirm_label = "Confirm rename"
        else:
            st.warning(
                f"Delete cluster **{pend['cluster']}** from **{label(pend['state'])}**? "
                f"All {len(members4)} PRM(s) below will be removed from the sheet. "
                "This cannot be undone from the app."
            )
            confirm_label = "Yes, delete cluster"
        st.dataframe(
            pd.DataFrame({"prm_name": members4}), hide_index=True, width="stretch",
        )
        c1, c2, _ = st.columns([1, 1, 4])
        c1.button(confirm_label, key="t4_go", on_click=t4_submit, type="primary")
        c2.button("Cancel", key="t4_cancel", on_click=back_to_edit, args=("t4",))