extends SceneTree
## Run via launch.py --headless --audio-driver Dummy --script tests/reactive_tracking_smoke.gd.

var arena: Node3D
var record: Dictionary = {}

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("REACTIVE_TRACKING_FAIL: " + message)
		quit(1)
	return condition

func target_angles() -> Vector2:
	var target: Dictionary = arena._current_target()
	return Vector2(target.yaw, target.pitch)

func run() -> void:
	arena = load("res://game/arena.gd").new()
	root.add_child(arena)
	await process_frame
	arena.round_finished.connect(func(finished): record = finished)
	var settings := {"seed":483,"duration_s":30.0,"speed_scale":1.0,"sound_enabled":false,"tracking_motion":"reactive"}
	arena.start_round("tracking",settings)
	var expected := PackedVector2Array()
	for step in range(1201):
		arena._elapsed = float(step) / 120.0
		arena._move_tracking_target()
		if step % 120 == 0: expected.append(target_angles())
	arena.start_round("tracking",settings)
	for second in range(11):
		arena._elapsed = float(second)
		arena._move_tracking_target()
		if not check(target_angles().is_equal_approx(expected[second]),"Same seed must recreate the path independently of update frequency"): return
	var max_speed := 0.0
	var max_acceleration := 0.0
	var h := 0.02
	for step in range(1,1499):
		var t := float(step) * h
		var previous: Vector2 = arena._reactive_position(t-h)
		var current: Vector2 = arena._reactive_position(t)
		var next: Vector2 = arena._reactive_position(t+h)
		if not check(absf(current.x) <= 24.0001 and absf(current.y) <= 10.0001,"Reactive targets must stay inside the angular bounds"): return
		max_speed = maxf(max_speed,(next-previous).length()/(2.0*h))
		max_acceleration = maxf(max_acceleration,(next-2.0*current+previous).length()/(h*h))
	if not check(max_speed < 17.4 and max_acceleration < 47.0,"Random direction changes must have bounded speed and acceleration"): return
	var other: Dictionary = settings.duplicate(true)
	other.seed = 997
	arena.start_round("tracking",other)
	arena._elapsed = 1.0
	arena._move_tracking_target()
	if not check(target_angles().distance_to(expected[1]) > 0.1,"Fresh seeds must create a different path"): return
	var fast: Dictionary = settings.duplicate(true)
	fast.speed_scale = 2.0
	arena.start_round("tracking",fast)
	arena._elapsed = 0.5
	arena._move_tracking_target()
	if not check(target_angles().is_equal_approx(expected[1]),"Speed multiplier must scale path time without changing the seeded path"): return
	print("PASS deterministic reactive path, fresh seeds, update independence, bounds, speed and acceleration")
	arena.start_round("tracking",settings)
	await create_timer(0.03).timeout
	arena.pause_round()
	var paused_target := target_angles()
	var paused_time: float = arena._elapsed
	await create_timer(0.05).timeout
	arena._process(1.0)
	if not check(arena._elapsed == paused_time and target_angles() == paused_target,"Reactive movement must freeze during pause"): return
	arena.resume_round()
	await create_timer(0.03).timeout
	if not check(arena._elapsed - paused_time < 0.08,"Reactive resume must exclude time paused"): return
	arena.stop_round()
	if not check(record.settings.tracking_motion == "reactive" and record.samples.size() >= 4,"Reactive mode and samples must be recorded"): return
	var last: Dictionary = record.samples.back()
	arena.start_replay(record)
	arena._advance_replay(float(record.duration_s))
	if not check(target_angles().is_equal_approx(Vector2(last.target_yaw,last.target_pitch)),"Replay must restore the recorded reactive target trajectory"): return
	var smooth: Dictionary = settings.duplicate(true)
	smooth.erase("tracking_motion")
	arena.start_round("tracking",smooth)
	arena._elapsed = 2.0
	arena._move_tracking_target()
	var phase: float = arena._tracking_phase
	var original := Vector2(21.0*sin(2.0*0.78+phase)+4.0*sin(2.0*1.73+phase*0.5),8.0*sin(2.0*0.91+phase*0.8)+3.0*sin(2.0*1.57))
	if not check(not arena._settings.has("tracking_motion") and target_angles().is_equal_approx(original),"Original smooth motion and default settings must remain unchanged"): return
	arena.stop_round()
	print("REACTIVE_TRACKING_PASS: pause/resume, recorded mode, replay, legacy smooth benchmark unchanged")
	root.remove_child(arena)
	arena.queue_free()
	await process_frame
	quit()
