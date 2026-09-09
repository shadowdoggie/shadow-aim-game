extends Node
## The companion derives the next step from saved rounds and approved coaching.

var host: Node
var next_step: Dictionary = {}
var _busy := false
var _button: Button
var _hint: Label
var _title: Label
var _detail: Label
var _error := ""

func setup(owner_node: Node) -> void:
	host = owner_node

static func mode_key(drill: String, config: Dictionary) -> String:
	return "reactive_tracking" if drill == "tracking" and config.get("tracking_motion", "smooth") == "reactive" else drill

func add_home_action(actions: Node, parent: Node) -> void:
	_button = host._button(actions,"Practice  →",start_next,true)
	_hint = host._paragraph(parent,"Checking your saved progress…",15)

func add_home_summary(parent: Node) -> void:
	_title = host._label(parent,"Your next useful step",25)
	_detail = host._paragraph(parent,"Checking your saved rounds and coaching.")
	_update_home()

func refresh() -> Dictionary:
	if _busy: return {}
	_busy = true
	_update_home()
	var response: Dictionary = await host._api("/training/next?settings="+JSON.stringify(host.settings).uri_encode())
	_busy = false
	if response.get("error") != null:
		_error = "Could not load your saved training step. Try again, or choose a drill below."
	else:
		_error = ""
		next_step = response.duplicate(true)
	_update_home()
	return response

func _update_home() -> void:
	if host.screen != "home": return
	var action: String = str(next_step.get("action",""))
	var complete: bool = bool(next_step.get("baseline_complete",false))
	if is_instance_valid(_button):
		_button.disabled = _busy
		_button.text = "Practice  →" if complete or action != "baseline" else "Start guided baseline  →" if next_step.get("missing_modes",[]).size() == 4 else "Finish guided baseline  →"
	if is_instance_valid(_hint):
		_hint.text = "Your saved progress picks the next step."
		if action == "baseline":
			var remaining: int = next_step.get("missing_modes",[]).size()
			_hint.text = "%d short rounds · clicking, smooth tracking, reactive tracking and switching" % remaining if remaining == 4 else "%d baseline round%s remaining" % [remaining,"" if remaining == 1 else "s"]
	if not is_instance_valid(_title) or not is_instance_valid(_detail): return
	if not _error.is_empty():
		_title.text = "Your progress stays saved"
		_detail.text = _error
		return
	var drill: String = str(next_step.get("drill",""))
	var config: Dictionary = next_step.get("settings",{}) if next_step.get("settings") is Dictionary else {}
	var title: String = host._drill_title(drill,config)
	match action:
		"baseline":
			_title.text = "Establish your baseline" if next_step.get("missing_modes",[]).size() == 4 else "Complete your baseline"
			_detail.text = "Measure all four aim skills once. Juniper can then guide your next practice."
		"coached_practice":
			_title.text = "Next: " + title
			_detail.text = "Practice your coach’s saved adjustment, then retest it."
		"continue_practice":
			_title.text = "Continue: " + title
			_detail.text = "Finish the practice block you started, using the same coaching and settings."
		"retest":
			_title.text = "Retest: " + title
			_detail.text = "Check what changed at your original baseline difficulty."
		"review":
			_title.text = "Keep practising"
			_detail.text = "Continue at the same settings while Juniper reviews your measurements."

func start_next() -> void:
	if _busy or host.is_playing or host.is_replaying: return
	var generation: int = host.page_generation
	var step := await refresh()
	if generation != host.page_generation or host.is_playing or host.is_replaying: return
	if step.is_empty() or step.get("error") != null:
		host._practice_while_reviewing()
		return
	if step.get("action","") == "review":
		await _review_and_keep_playing(step)
	else: apply_step(step)

func _review_and_keep_playing(step: Dictionary) -> void:
	var record: Dictionary = step.get("record",{}) if step.get("record") is Dictionary else {}
	var report: Dictionary = step.get("report",{}) if step.get("report") is Dictionary else {}
	var record_id: String = str(step.get("record_id",""))
	if record.is_empty() and str(host.last_record.get("id","")) == record_id:
		record = host.last_record.duplicate(true)
		report = host.last_report.duplicate(true)
	if record.get("drill","") not in host.DRILLS or not record.get("settings") is Dictionary: return
	if host.job_id.is_empty() and not host.submitting_coach and not report.is_empty():
		# Saved compact snapshots suffice; do not download a replay or wait for
		# a model response before starting the player's next round.
		if str(host.last_record.get("id","")) != record_id:
			host.last_record = record.duplicate(true)
			host.last_report = report.duplicate(true)
		host._request_coaching()
	host._practice_while_reviewing(record)

func review_completed_baseline() -> void:
	var record_id: String = str(host.last_record.get("id",""))
	var step := await refresh()
	if host.screen != "results" or host.is_playing or host.is_replaying or str(host.last_record.get("id","")) != record_id: return
	if step.get("action","") == "review": apply_step(step)

func apply_step(step: Dictionary) -> void:
	var action: String = str(step.get("action",""))
	if action == "baseline":
		host._start_baseline(step.get("missing_modes",[]),str(step.get("cycle_id","")))
		return
	if action == "review":
		var record_id: String = str(step.get("record_id",""))
		if record_id.is_empty(): return
		if str(host.last_record.get("id","")) == record_id and not host.last_report.is_empty():
			host._request_coaching()
			return
		await host._load_session(record_id)
		if host.screen == "results" and str(host.last_record.get("id","")) == record_id:
			host._request_coaching()
		return
	if action not in ["coached_practice","continue_practice","retest"]: return
	var config: Dictionary = step.get("settings",{}) if step.get("settings") is Dictionary else {}
	var baseline: Dictionary = step.get("baseline_record",{}) if step.get("baseline_record") is Dictionary else {}
	var advice: Dictionary = step.get("recommendation",{}) if step.get("recommendation") is Dictionary else {}
	var drill: String = str(step.get("drill",""))
	if drill not in host.DRILLS or config.is_empty() or baseline.is_empty() or advice.is_empty():
		_error = "Your saved plan is incomplete. Open History to review the round."
		_update_home()
		return
	host.guided = true
	host.recommendation = advice.duplicate(true)
	host.recommendation_record_id = str(step.get("source_coaching_record_id",""))
	host.source_coaching_record_id = host.recommendation_record_id
	host.cycle_id = str(step.get("cycle_id",""))
	if host.cycle_id.is_empty(): host.cycle_id = host._new_cycle_id()
	host.benchmarks[mode_key(drill,baseline.get("settings",{}))] = baseline.duplicate(true)
	host.stage = "retest" if action == "retest" else "practice"
	host.pending_rounds.clear()
	var count := 1 if action == "retest" else clampi(int(step.get("remaining_rounds",2)),1,2)
	for i in count: host.pending_rounds.append({"drill":drill,"settings":config.duplicate(true)})
	host._next_round()
