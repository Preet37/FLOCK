//! Flock's SpacetimeDB module: the same store the JSON files give us, as three tables and four reducers.
//!
//! Tables (all public, so the dashboard can subscribe later):
//!   finding       one row per finding; `row` is the finding JSON exactly as the Python side knows it
//!   audit_log     append-only; `entry` is the gatekeeper's log entry JSON. No reducer updates or deletes a row
//!                 except `reset`, which only the demo uses.
//!   agent_status  append-only live agent feed (spawn / working / done / failed events), `event` JSON
//!
//! Reducers (every write goes through one of these, each is one transaction):
//!   put_new(finding_id, state, row)                         insert a NEW finding
//!   commit(finding_id, state, row, subject_id, entry)       upsert the finding (skip when finding_id is empty:
//!                                                           a denied attempt) AND insert the log entry together
//!   agent_event(event)                                      append a live-feed event
//!   reset()                                                 wipe all three tables (fresh demo run only)
//!
//! The Python side (flock/store.py SpacetimeStore) calls these over SpacetimeDB's HTTP API and reads with SQL.
use spacetimedb::{ReducerContext, Table};

#[spacetimedb::table(accessor = finding, public)]
pub struct Finding {
    #[primary_key]
    finding_id: String,
    #[index(btree)]
    state: String,
    row: String,
}

#[spacetimedb::table(accessor = audit_log, public)]
pub struct AuditLog {
    #[primary_key]
    #[auto_inc]
    id: u64,
    #[index(btree)]
    subject_id: String,
    entry: String,
}

#[spacetimedb::table(accessor = agent_status, public)]
pub struct AgentStatus {
    #[primary_key]
    #[auto_inc]
    seq: u64,
    event: String,
}

fn upsert(ctx: &ReducerContext, finding_id: String, state: String, row: String) {
    if ctx.db.finding().finding_id().find(&finding_id).is_some() {
        ctx.db.finding().finding_id().update(Finding { finding_id, state, row });
    } else {
        ctx.db.finding().insert(Finding { finding_id, state, row });
    }
}

#[spacetimedb::reducer]
pub fn put_new(ctx: &ReducerContext, finding_id: String, state: String, row: String) -> Result<(), String> {
    if state != "NEW" {
        return Err(format!("put_new only accepts NEW findings, got {state}"));
    }
    upsert(ctx, finding_id, state, row);
    Ok(())
}

#[spacetimedb::reducer]
pub fn commit(ctx: &ReducerContext, finding_id: String, state: String, row: String, subject_id: String, entry: String) {
    if !finding_id.is_empty() {
        upsert(ctx, finding_id, state, row);
    }
    ctx.db.audit_log().insert(AuditLog { id: 0, subject_id, entry });
}

#[spacetimedb::reducer]
pub fn agent_event(ctx: &ReducerContext, event: String) {
    ctx.db.agent_status().insert(AgentStatus { seq: 0, event });
}

#[spacetimedb::reducer]
pub fn reset(ctx: &ReducerContext) {
    let ids: Vec<String> = ctx.db.finding().iter().map(|f| f.finding_id).collect();
    for id in ids {
        ctx.db.finding().finding_id().delete(&id);
    }
    let log_ids: Vec<u64> = ctx.db.audit_log().iter().map(|e| e.id).collect();
    for id in log_ids {
        ctx.db.audit_log().id().delete(&id);
    }
    let seqs: Vec<u64> = ctx.db.agent_status().iter().map(|e| e.seq).collect();
    for s in seqs {
        ctx.db.agent_status().seq().delete(&s);
    }
}
