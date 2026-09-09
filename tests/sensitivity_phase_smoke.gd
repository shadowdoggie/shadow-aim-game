extends SceneTree
## Verify warmup/scoring separation without waiting or contacting the companion.

class PhasePanel:
	extends "res://game/sensitivity_panel.gd"
	var submitted: Array[Dictionary] = []
	func _submit_round() -> void:
		submitted.append(pending_record.duplicate(true))

class FakeArena:
	extends Node
	var starts: Array[Dictionary] = []
	func start_round(drill: String, settings: Dictionary) -> void:
		starts.append({"drill":drill,"settings":settings.duplicate(true)})

class PhaseHost:
	extends Node
	var is_playing := false
	var stage := "sensitivity"
	var current_drill := ""
	var current_round_settings: Dictionary = {}
	var crosshair: Label
	var hud: Label
	var arena: FakeArena
	func _ready() -> void:
		crosshair = Label.new()
		hud = Label.new()
		arena = FakeArena.new()
		add_child(crosshair)
		add_child(hud)
		add_child(arena)
	func _begin_round(drill: String, settings: Dictionary) -> void:
		is_playing = true
		arena.start_round(drill,settings)


func _initialize() -> void:
	call_deferred("run")


func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("SENSITIVITY_PHASE_FAIL: " + message)
		quit(1)
	return condition


func run() -> void:
	var host := PhaseHost.new()
	root.add_child(host)
	var panel := PhasePanel.new()
	host.add_child(panel)
	panel.setup(host)
	panel.active = true
	var settings := {"duration_s":20.0,"seed":1234,"sensitivity_deg_per_count":0.056,"fov":103.0,"target_scale":1.0,"speed_scale":1.0}
	panel.experiment = {"id":"phase-test","record_ids":[],"blocks":[{"index":0,"candidate_id":"lower","drill":"clicking","settings":settings}]}
	panel.labels = {"lower":"B"}
	panel._begin_block()
	if not check(host.arena.starts.size() == 1 and host.arena.starts[0].settings.duration_s == 5.0,"Warmup remains exactly five seconds"): return
	var warmup_hud := panel.hud_text({"drill":"clicking","remaining_s":3.1,"hits":8,"accuracy":100.0})
	if not check(warmup_hud.contains("WARMUP · NOT SCORED") and warmup_hud.contains("Scoring begins in 4s") and warmup_hud.contains("targets reset"),"Warmup explicitly counts down to the target reset"): return
	if not check(not warmup_hud.contains("hits") and not warmup_hud.contains("accuracy"),"Unscored warmup cannot look like a score being lost"): return
	await panel.on_round_finished({"id":"warmup","completed":true,"shots":[{"hit":true}]})
	if not check(not panel.warming and panel.submitted.is_empty(),"Warmup result is discarded instead of submitted"): return
	if not check(host.arena.starts.size() == 2 and host.arena.starts[1].settings == settings,"Measurement starts once with unchanged matched settings and seed"): return
	var scored_hud := panel.hud_text({"drill":"clicking","remaining_s":20.0,"hits":0,"accuracy":0.0})
	if not check(scored_hud.contains("SCORING STARTED · 20s left") and not scored_hud.contains("WARMUP"),"Scoring transition is clearly marked"): return
	if not check(panel.hud_text({"drill":"tracking","remaining_s":10.0}).contains("Hold left mouse"),"Tracking controls remain visible"): return
	await panel.on_round_finished({"id":"measured","completed":true,"settings":settings,"shots":[]})
	if not check(panel.submitted.size() == 1 and panel.submitted[0].id == "measured" and panel.submitted[0].sensitivity_context.block_index == 0,"Only the measured block is submitted once"): return
	print("SENSITIVITY_PHASE_SMOKE_PASS")
	root.remove_child(host)
	host.queue_free()
	await process_frame
	quit()
