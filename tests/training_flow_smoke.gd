extends SceneTree
## Saved-plan fixtures only; no account, microphone, inference or gameplay input.

const TrainingFlow = preload("res://game/training_flow.gd")

class RoutingHost:
	extends "res://game/main.gd"
	var step: Dictionary = {}
	var starts: Array[Dictionary] = []
	var reviewed := ""
	var review_requests := 0
	var requested_path := ""
	func _ready() -> void: pass
	func _begin_round(drill: String, config: Dictionary) -> void:
		starts.append({"drill":drill,"settings":config.duplicate(true)})
	func _api(path: String, _method: int = HTTPClient.METHOD_GET, _payload: Dictionary = {}) -> Dictionary:
		requested_path = path
		await get_tree().process_frame
		return step.duplicate(true)
	func _load_session(id: String) -> void:
		reviewed = id
		last_record = {"id":id}
		screen = "results"
	func _request_coaching(_question: String = "") -> void:
		review_requests += 1

func _initialize() -> void:
	call_deferred("run")
	create_timer(5.0).timeout.connect(func(): quit(1))

func run() -> void:
	var host := RoutingHost.new()
	root.add_child(host)
	var flow := TrainingFlow.new()
	host.add_child(flow)
	flow.setup(host)
	host.step = {"action":"baseline","baseline_complete":false,"missing_modes":["clicking","tracking","reactive_tracking","switching"],"cycle_id":""}
	await flow.start_next()
	while not host.pending_rounds.is_empty(): host._next_round()
	assert(host.starts.size() == 4 and host.baseline_round_total == 4,"First guided baseline must include four rounds")
	assert(host.starts[1].drill == "tracking" and host.starts[1].settings.tracking_motion == "smooth")
	assert(host.starts[2].drill == "tracking" and host.starts[2].settings.tracking_motion == "reactive","Reactive tracking must have its own measured baseline")
	assert(host.starts[3].drill == "switching")
	assert(host.requested_path.begins_with("/training/next?settings="),"Next-step selection must include the current aim profile")
	host.step = {"action":"baseline","baseline_complete":false,"missing_modes":["reactive_tracking"],"cycle_id":"saved-baseline"}
	await flow.start_next()
	assert(host.cycle_id == "saved-baseline" and host.baseline_round_total == 1 and host.starts.back().settings.tracking_motion == "reactive","An existing baseline must resume only its missing mode")
	var original: Dictionary = host.settings.duplicate(true)
	original.tracking_motion = "reactive"
	var approved := {"drill":"tracking","parameters":{"duration_s":30,"target_scale":1.25,"speed_scale":0.8}}
	var practice: Dictionary = original.duplicate(true)
	practice.merge(approved.parameters,true)
	var saved := {"action":"continue_practice","baseline_complete":true,"missing_modes":[],"cycle_id":"saved-practice","record_id":"practice-one","source_coaching_record_id":"approved-review","recommendation":approved,"baseline_record":{"id":"reactive-baseline","drill":"tracking","settings":original},"remaining_rounds":1,"settings":practice,"drill":"tracking","mode":"reactive_tracking"}
	host.step = saved
	await flow.start_next()
	assert(host.stage == "practice" and host.cycle_id == "saved-practice" and host.source_coaching_record_id == "approved-review","Practice must resume its saved coaching relationship")
	assert(host.pending_rounds.is_empty() and host.starts.back().settings == practice,"Only the remaining practice round must use the exact approved settings")
	assert(host.benchmarks.reactive_tracking.id == "reactive-baseline" and not host.benchmarks.has("tracking"),"Reactive and smooth baselines must not overwrite one another")
	host.step = saved.duplicate(true)
	host.step.action = "retest"
	host.step.settings = original
	await flow.start_next()
	assert(host.stage == "retest" and host.starts.back().settings == original and host.cycle_id == "saved-practice","Retest must restore its original settings and saved cycle")
	# A fresh native helper obtains the same completed state from persisted data.
	var reopened := TrainingFlow.new()
	host.add_child(reopened)
	reopened.setup(host)
	await reopened.refresh()
	assert(reopened.next_step.baseline_complete and reopened.next_step.action == "retest","Reopening must restore progress instead of restarting baseline")
	host.step = {"action":"review","baseline_complete":true,"record_id":"measured-weak-baseline"}
	host.screen = "results"
	host.last_record = {"id":"final-switching-baseline"}
	await flow.review_completed_baseline()
	await process_frame
	assert(host.reviewed == "measured-weak-baseline" and host.review_requests == 1,"Final baseline must request only the backend-selected measurement review")
	host.queue_free()
	await process_frame
	print("TRAINING_FLOW_SMOKE_PASS: four distinct baseline modes, partial baseline resume, approved saved practice, exact retest, restart restoration, single selected baseline review")
	quit()
