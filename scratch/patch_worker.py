with open('webapp/worker.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Update _research_inputs to initialize search_budget from config.SPECULATIVE_SEARCH_BUDGET
# and read existing rejected_candidates from item.research_result
target_inputs = """def _research_inputs(item: BatchItem, run: ApplicationRun, candidate_profile: str) -> dict[str, Any]:
    return {
        "messages": [],
        "seed_query": (
            (item.target_company or item.target_domain)
            if item.mode == "open_role"
            else None
        ),
        "candidate_profile": item.candidate_profile or candidate_profile,
        "mode": item.mode,
        "target_seniority": item.target_seniority,
        "target_domain_keywords": item.target_domain_keywords,
        "location_preference": item.location_preference,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
        "search_attempt": 0,
        "search_budget": legacy_config.SPECULATIVE_SEARCH_BUDGET,
        "retry_counted": False,
        "rejected_candidates": [],
        "research_run_id": run.id,
        "owner_id": run.owner_id,
        "contact_attempts": 0,
        "research_outcome": None,
        "research_error": None,
        "research_cache_hit": False,
    }"""

replacement_inputs = """def _research_inputs(item: BatchItem, run: ApplicationRun, candidate_profile: str) -> dict[str, Any]:
    existing_result = item.research_result or {}
    existing_rejected = list(existing_result.get("rejected_candidates") or [])
    current_attempt = int(existing_result.get("search_attempt") or 0)
    configured_budget = getattr(legacy_config, "SPECULATIVE_SEARCH_BUDGET", 4)
    configured_max_attempts = getattr(legacy_config, "MAX_RESEARCH_ATTEMPTS", 5)
    effective_budget = max(configured_budget, configured_max_attempts)
    return {
        "messages": [],
        "seed_query": (
            (item.target_company or item.target_domain)
            if item.mode == "open_role"
            else None
        ),
        "candidate_profile": item.candidate_profile or candidate_profile,
        "mode": item.mode,
        "target_seniority": item.target_seniority,
        "target_domain_keywords": item.target_domain_keywords,
        "location_preference": item.location_preference,
        "retry_count": 0,
        "max_retries": legacy_config.MAX_RETRIES,
        "search_attempt": current_attempt,
        "search_budget": effective_budget,
        "retry_counted": False,
        "rejected_candidates": existing_rejected,
        "research_run_id": run.id,
        "owner_id": run.owner_id,
        "contact_attempts": 0,
        "research_outcome": None,
        "research_error": None,
        "research_cache_hit": False,
    }"""

assert target_inputs in text, "target_inputs not found in worker.py"
text = text.replace(target_inputs, replacement_inputs, 1)

# 2. Update _process_research_decision to support speculative continuation upon rejection
target_decision = """        if decision == "rejected":
            company = (item.research_result or {}).get("company_name")
            domain = (item.research_result or {}).get("company_domain")
            if company:
                key = normalize_domain(domain) or normalize_company_name(company)
                existing = session.scalar(select(Exclusion.id).where(
                    Exclusion.owner_id == run.owner_id,
                    Exclusion.normalized_key.in_(
                        [f"domain:{key}", f"name:{normalize_company_name(company)}"]
                        if normalize_domain(domain)
                        else [f"name:{key}"]
                    ),
                ))
                if not existing:
                    session.add(Exclusion(
                        owner_id=run.owner_id,
                        normalized_key=f"domain:{key}" if normalize_domain(domain) else f"name:{key}",
                        rejection_reason="Research candidate rejected by user.",
                        created_at=utc_now(),
                    ))
            item.status = "rejected"
            run.status = "completed"
            run.progress = 100
            run.finished_at = utc_now()
            event_type = "research_rejection_applied"
        else:"""

replacement_decision = """        if decision == "rejected":
            company = (item.research_result or {}).get("company_name")
            domain = (item.research_result or {}).get("company_domain")
            if company:
                key = normalize_domain(domain) or normalize_company_name(company)
                existing = session.scalar(select(Exclusion.id).where(
                    Exclusion.owner_id == run.owner_id,
                    Exclusion.normalized_key.in_(
                        [f"domain:{key}", f"name:{normalize_company_name(company)}"]
                        if normalize_domain(domain)
                        else [f"name:{key}"]
                    ),
                ))
                if not existing:
                    session.add(Exclusion(
                        owner_id=run.owner_id,
                        normalized_key=f"domain:{key}" if normalize_domain(domain) else f"name:{key}",
                        rejection_reason="Research candidate rejected by user.",
                        created_at=utc_now(),
                    ))

            # Maintain rejected candidate history in item.research_result
            res = dict(item.research_result or {})
            rejected_list = list(res.get("rejected_candidates") or [])
            if company or res.get("job_role"):
                rejected_list.append({
                    "status": "rejected",
                    "company_name": company or "",
                    "company_domain": domain or "",
                    "job_role": res.get("job_role") or "",
                    "reason": "User explicitly rejected this candidate opportunity.",
                })
            res["rejected_candidates"] = rejected_list
            item.research_result = res

            current_attempt = int(res.get("search_attempt") or 1)
            configured_max = getattr(legacy_config, "MAX_RESEARCH_ATTEMPTS", 5)
            # In speculative discovery mode, check if we can continue searching for another opportunity
            if item.mode == "speculative" and current_attempt < configured_max:
                item.status = "queued"
                item.research_decision = None
                item.failure_code = None
                run.attempt += 1
                run.status = "queued"
                run.phase = "research"
                run.progress = 10
                run.finished_at = None
                # Enqueue research_item to continue searching
                continuation_dedupe = f"research-continue:{run.id}:{run.attempt}"
                session.add(OutboxMessage(
                    id=str(uuid.uuid4()),
                    topic="research_item",
                    dedupe_key=continuation_dedupe,
                    payload={
                        "run_id": run.id,
                        "batch_id": run.batch_id,
                        "item_id": item.id,
                        "owner_id": run.owner_id,
                        "thread_id": f"{run.thread_id}:{run.attempt}",
                    },
                    available_at=utc_now(),
                    attempts=0,
                    created_at=utc_now(),
                ))
                event_type = "research_rejection_continuation_queued"
            else:
                item.status = "rejected" if item.mode != "speculative" else "ineligible"
                item.failure_code = "search_exhausted" if item.mode == "speculative" else None
                run.status = "completed"
                run.progress = 100
                run.finished_at = utc_now()
                event_type = "research_rejection_applied"
        else:"""

assert target_decision in text, "target_decision not found in worker.py"
text = text.replace(target_decision, replacement_decision, 1)

# 3. Add idempotency guard to _process_drafts
target_drafts = """def _process_drafts(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.research_decision != "approved":
        raise ValueError("research_not_approved")
    if item.status in {"draft_review", "completed"}:
        return
    if not item.cv_artifact_id:
        raise ValueError("cv_artifact_missing")"""

replacement_drafts = """def _process_drafts(factory, payload: dict[str, Any], checkpointer) -> None:
    run, item = _state_snapshot(factory, payload["run_id"], payload["item_id"])
    if item.research_decision != "approved":
        raise ValueError("research_not_approved")
    if item.status in {"draft_review", "completed"}:
        return
    if not item.cv_artifact_id:
        raise ValueError("cv_artifact_missing")

    # Guard against duplicate concurrent execution: check if drafts already exist for this run
    with factory() as check_session:
        existing_draft_count = check_session.scalar(
            select(DraftVersion.id).where(
                DraftVersion.batch_id == run.batch_id,
                DraftVersion.item_id == item.id,
                DraftVersion.owner_id == run.owner_id,
            ).limit(1)
        )
        if existing_draft_count is not None:
            # Already drafted; ensure state is reflected
            with factory.begin() as update_session:
                cur_item = update_session.get(BatchItem, item.id)
                cur_run = update_session.get(ApplicationRun, run.id)
                if cur_item and cur_item.status == "drafting":
                    cur_item.status = "draft_review"
                if cur_run and cur_run.status == "running":
                    cur_run.status = "paused"
                    cur_run.phase = "draft_review"
            return"""

assert target_drafts in text, "target_drafts not found in worker.py"
text = text.replace(target_drafts, replacement_drafts, 1)

# 4. In _finish_research, handle no_suitable_opportunity when budget exhausted
target_exhausted = """        item.status = "research_review" if paused else (
            "ineligible" if outcome in {
                "no_matching_role",
                "no_usable_contact",
                "search_exhausted",
            } else "failed"
        )
        item.failure_code = (
            None if paused else (
                "search_exhausted" if outcome == "search_exhausted"
                else "no_matching_role" if outcome == "no_matching_role"
                else "no_usable_contact" if outcome == "no_usable_contact"
                else "research_failed"
            )
        )"""

replacement_exhausted = """        item.status = "research_review" if paused else (
            "ineligible" if outcome in {
                "no_matching_role",
                "no_usable_contact",
                "search_exhausted",
            } else "failed"
        )
        item.failure_code = (
            None if paused else (
                "no_suitable_opportunity" if outcome == "search_exhausted"
                else "no_matching_role" if outcome == "no_matching_role"
                else "no_usable_contact" if outcome == "no_usable_contact"
                else "research_failed"
            )
        )"""

assert target_exhausted in text, "target_exhausted not found in worker.py"
text = text.replace(target_exhausted, replacement_exhausted, 1)

with open('webapp/worker.py', 'w', encoding='utf-8', newline='\n') as f:
    f.write(text)

print("worker.py updated successfully with continuation, draft idempotency, and clean failure codes!")

