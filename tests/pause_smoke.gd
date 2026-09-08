extends SceneTree
## Run through launch.py --headless --audio-driver Dummy --script tests/pause_smoke.gd.

class Heartbeat:
	extends Node
	var frames := 0
	func _process(_delta: float) -> void:
		frames += 1

var arena: Node3D
var ended: Dictionary = {}
var pause_requests := 0

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("PAUSE_SMOKE_FAIL: " + message)
		quit(1)
	return condition

func wait_wall_time(seconds: float) -> void:
	# Pause bookkeeping uses monotonic wall time; headless frame deltas can run
	# ahead of that clock on CI, so a SceneTreeTimer is not the same measurement.
	var deadline := Time.get_ticks_usec() + int(seconds * 1000000.0)
	while Time.get_ticks_usec() < deadline:
		await process_frame

func run() -> void:
	arena = load("res://game/arena.gd").new()
	root.add_child(arena)
	var heartbeat := Heartbeat.new()
	root.add_child(heartbeat)
	await process_frame
	arena.pause_requested.connect(func(): pause_requests += 1)
	arena.round_finished.connect(func(record): ended = record)
	var settings := {"seed":483,"duration_s":5.0,"sensitivity_deg_per_count":0.05,"sound_enabled":false}
	arena.start_round("tracking", settings)
	await create_timer(0.05).timeout
	var escape := InputEventKey.new()
	escape.keycode = KEY_ESCAPE
	escape.pressed = true
	arena._input(escape)
	if not check(arena.paused and arena._running and pause_requests == 1 and ended.is_empty(),"Escape must pause the current round without ending it"): return
	if not check(Input.mouse_mode == Input.MOUSE_MODE_VISIBLE,"Pause must release the pointer"): return
	var frozen_elapsed: float = arena._elapsed
	var frozen_target: Vector3 = arena._current_target().mesh.position
	var frozen_camera: Vector3 = arena.camera.rotation
	var sample_count: int = arena._record.samples.size()
	var settings_before: Dictionary = arena._settings.duplicate(true)
	var before_frames := heartbeat.frames
	await wait_wall_time(0.15)
	var motion := InputEventMouseMotion.new()
	motion.screen_relative = Vector2(400, -200)
	arena._input(motion)
	var fire := InputEventMouseButton.new()
	fire.button_index = MOUSE_BUTTON_LEFT
	fire.pressed = true
	Input.parse_input_event(fire)
	arena._input(fire)
	arena._fire_shot()
	arena._process(2.0)
	if not check(arena._elapsed == frozen_elapsed and arena._current_target().mesh.position == frozen_target and arena.camera.rotation == frozen_camera,"Elapsed time, target and camera must remain frozen"): return
	if not check(arena._record.samples.size() == sample_count and arena._record.shots.is_empty(),"Paused motion and fire must not enter telemetry"): return
	if not check(heartbeat.frames > before_frames and not paused,"Other nodes must keep processing for live voice"): return
	arena.resume_round()
	if not check(not arena.paused and arena._running and arena._held,"Resume must preserve the round and reread held fire"): return
	if not check(arena._settings == settings_before and arena._current_target().mesh.position == frozen_target and arena.camera.rotation == frozen_camera,"Resume must preserve aim, target, settings and seed"): return
	if DisplayServer.get_name() != "headless":
		if not check(Input.mouse_mode == Input.MOUSE_MODE_CAPTURED,"Resume must recapture the pointer"): return
	await create_timer(0.04).timeout
	if not check(arena._elapsed - frozen_elapsed < 0.10,"Time in the pause menu must not enter active elapsed time"): return
	if not check(arena._record.pause_count == 1 and arena._record.paused_seconds >= 0.14,"Record must disclose the excluded pause"): return
	var previous_time := -1.0
	for sample in arena._record.samples:
		if not check(float(sample.t) >= previous_time and float(sample.frame_ms) < 100.0,"Resumed samples must be monotonic without a pause-sized timing spike"): return
		previous_time = float(sample.t)
	arena._notification(Node.NOTIFICATION_APPLICATION_FOCUS_OUT)
	if not check(arena.paused and pause_requests == 2 and ended.is_empty(),"Focus loss must pause without discarding the round"): return
	frozen_elapsed = arena._elapsed
	await wait_wall_time(0.04)
	arena.stop_round()
	if not check(not arena._running and not arena.paused and not ended.completed and ended.duration_s == frozen_elapsed,"Ending from pause must emit an incomplete round with active duration only"): return
	if not check(ended.pause_count == 2 and ended.paused_seconds >= 0.18,"Ending while paused must retain complete pause bookkeeping"): return
	fire.pressed = false
	Input.parse_input_event(fire)
	arena.start_round("clicking", settings)
	if not check(not arena.paused and arena._record.pause_count == 0 and arena._record.paused_seconds == 0.0,"A fresh round must reset pause state"): return
	arena.stop_round()
	print("PAUSE_SMOKE_PASS: Escape/focus pause, frozen arena and telemetry, active voice loop, resumed timing, held fire, incomplete end")
	root.remove_child(arena)
	arena.queue_free()
	heartbeat.queue_free()
	await process_frame
	quit()
