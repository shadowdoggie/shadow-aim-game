extends SceneTree
## No inference or microphone. Run through launch.py with an isolated AIMCOACH_DATA_DIR.

class WorkflowHost:
	extends "res://game/main.gd"
	var submitted: Array[Dictionary] = []
	var sensitivity_submitted: Array[Dictionary] = []
	var settings_writes := 0

	func _load_settings() -> void:
		pass

	func _save_settings() -> void:
		settings_writes += 1

	func _retry_pending() -> void:
		pass

	func _api(path: String, _method: int = HTTPClient.METHOD_GET, payload: Dictionary = {}) -> Dictionary:
		if path == "/health": return {"status":"ok","connection":"ready"}
		if path == "/voice/context": return {"ok":true}
		if path == "/sessions":
			submitted.append(payload.duplicate(true))
			return {"report":{"record_id":payload.id,"benchmark_key":"matched","metrics":{"accuracy_pct":{"value":90.0,"unit":"%"}}},"comparison":{"kind":"baseline","metrics":{"accuracy_pct":{"previous":7.0,"current":90.0,"delta":83.0}}}}
		if path.ends_with("/round"):
			sensitivity_submitted.append(payload.record.duplicate(true))
			var saved: Dictionary = sensitivity.experiment.duplicate(true)
			saved.record_ids.append(payload.record.id)
			return {"experiment":saved}
		return {"error":"No external service in workflow smoke test"}

var host: WorkflowHost

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("UI_WORKFLOWS_FAIL: " + message)
		quit(1)
	return condition

func finish_round() -> void:
	host.arena._elapsed = host.arena._duration
	host.arena._finish_round(true)

func run() -> void:
	host = WorkflowHost.new()
	root.add_child(host)
	await process_frame
	if not check(host.voice.state == "off" and not host.voice.is_active and host.voice._microphone == null,"Startup must leave the microphone off"): return
	var baseline_settings: Dictionary = host.settings.duplicate(true)
	baseline_settings.duration_s = 5.0
	baseline_settings.seed = 190
	host.last_record = {"id":"baseline-source","drill":"clicking","settings":baseline_settings,"samples":[],"completed":true}
	host.last_report = {"record_id":"baseline-source","benchmark_key":"matched","metrics":{"accuracy_pct":{"value":95.0,"unit":"%"}}}
	host.recommendation_record_id = "coaching-source"
	host.recommendation = {"drill":"clicking","cue":"Settle before firing","parameters":{"duration_s":5.0,"target_scale":1.2,"speed_scale":1.0}}
	host._practice_prescription()
	await create_timer(2.1).timeout
	if not check(host.arena._running,"Prescribed practice must begin"): return
	var cycle: String = host.active_training_context.cycle_id
	if not check(host.active_training_context.baseline_record_id == "baseline-source" and host.active_training_context.source_coaching_record_id == "coaching-source","Practice must identify its baseline and source advice"): return
	finish_round()
	if not check(host.submitted.size() == 1 and host.submitted[0].training_context.kind == "practice","Practice metadata must reach storage"): return
	host._next_round()
	await create_timer(2.1).timeout
	finish_round()
	host._retest()
	await create_timer(2.1).timeout
	if not check(host.arena._running and host.arena._settings.target_scale == baseline_settings.target_scale and host.arena._settings.seed != 190,"Retest must restore difficulty with a fresh sequence"): return
	finish_round()
	var retest: Dictionary = host.submitted.back()
	if not check(retest.training_context.kind == "retest" and retest.training_context.cycle_id == cycle and retest.training_context.baseline_record_id == "baseline-source" and retest.training_context.source_coaching_record_id == "coaching-source","Retest must preserve the full practice relationship"): return
	if not check(host.comparison.metrics.accuracy_pct.previous == 7.0,"The companion comparison must remain authoritative over cached UI metrics"): return
	print("PASS practice/retest metadata, restored difficulty, fresh seed, authoritative comparison")
	var scored: Dictionary = baseline_settings.duplicate(true)
	scored.seed = 483
	scored.duration_s = 20.0
	scored.sensitivity_deg_per_count = 0.05
	var block := {"candidate_id":"lower","drill":"tracking","settings":scored,"index":0}
	var second: Dictionary = block.duplicate(true)
	second.index = 1
	host.sensitivity.active = true
	host.sensitivity.experiment = {"id":"workflow-sensitivity","blocks":[block,second],"record_ids":[]}
	host.sensitivity.labels = {"lower":"A"}
	host.sensitivity._begin_block()
	await create_timer(2.1).timeout
	if not check(host.arena._settings.seed == 483 + 7919 and host.arena._duration == 5.0,"Warmup must use its own preserved sequence"): return
	finish_round()
	if not check(host.arena._running and host.arena._settings.seed == 483 and host.arena._duration == 20.0,"Measured round must start immediately with the matched seed"): return
	if not check(host.submitted.size() == 3 and host.sensitivity_submitted.is_empty(),"Warmup must never enter saved measurements"): return
	finish_round()
	if not check(host.sensitivity_submitted.size() == 1 and host.sensitivity_submitted[0].training_context.kind == "sensitivity" and host.sensitivity_submitted[0].sensitivity_context.block_index == 0,"Only the scored sensitivity block must use its experiment route"): return
	host.sensitivity._begin_block()
	await create_timer(2.1).timeout
	host.arena.stop_round()
	if not check(not host.sensitivity.active and host.submitted.size() == 3 and host.sensitivity_submitted.size() == 1,"Aborted sensitivity rounds must not save as ordinary practice"): return
	if not check(host.settings.sensitivity_deg_per_count == 0.07 and host.settings_writes == 0,"Trial sensitivity must not change the saved profile"): return
	if not check(host.voice.state == "off" and not host.voice.is_active and host.voice._microphone == null,"Navigation and practice must not activate voice implicitly"): return
	print("UI_WORKFLOWS_PASS: scored sensitivity seeds, warmup exclusion, cancellation, profile unchanged, explicit voice start")
	root.remove_child(host)
	host.queue_free()
	await process_frame
	quit()
