extends SceneTree
## Run: ./launch.py --headless --script res://tests/arena_smoke.gd
## Add -- --snapshot --profile with a display for an image / five-second frame sample.
## Add -- --export-traces for actual engine telemetry from scripted mouse inputs.

var arena
var finished: Dictionary = {}

func _initialize() -> void:
	call_deferred("run")

func capture(record: Dictionary) -> void:
	finished = record

func run() -> void:
	arena = load("res://game/arena.gd").new()
	root.add_child(arena)
	await process_frame
	arena.round_finished.connect(capture)
	for drill in ["clicking", "switching", "tracking"]:
		if drill == "tracking":
			var held_before_start := InputEventMouseButton.new()
			held_before_start.button_index = MOUSE_BUTTON_LEFT
			held_before_start.pressed = true
			Input.parse_input_event(held_before_start)
		arena.start_round(drill, {"seed": 734, "duration_s": 5.0, "sound_enabled": false})
		var target: Dictionary = arena._current_target()
		var initial_id: String = target.id
		arena._yaw = target.yaw
		arena._pitch = target.pitch
		arena._apply_camera()
		assert(arena._angular_error(target) < 0.0001, "Angular target check failed")
		var camera_forward: Vector3 = -arena.camera.global_basis.z
		var to_target: Vector3 = (target.mesh.global_position - arena.camera.global_position).normalized()
		assert(camera_forward.angle_to(to_target) < 0.0001, "Camera / telemetry mismatch")
		if drill != "tracking":
			arena._fire_shot()
			assert(arena._record.shots.size() == 1 and arena._record.shots[0].hit)
			assert(arena._current_target().id != initial_id)
			assert(arena._record.events.size() == 4)
			assert(arena._record.events[3].type == "spawn")
			if drill == "switching":
				assert(arena._targets.size() == 3)
				assert(arena._record.events[3].targets_snapshot.size() == 3)
		else:
			assert(arena._held, "Holding fire through the countdown must engage tracking")
			assert(arena._record.samples[0].engaged, "Initial tracking sample must preserve held fire")
			arena._capture_sample(8.33)
			assert(arena._record.samples.back().on_target)
			assert(arena._record.samples.back().engaged)
		arena._start_usec -= 6000000
		arena._process(0.00833)
		assert(finished.completed and finished.duration_s == 5.0)
		assert(finished.settings.duration_s == 5.0)
		arena.start_replay(finished)
		arena._advance_replay(6.0)
		assert(not arena._replaying)
		print("PASS ", drill, " samples=", finished.samples.size())
	var release := InputEventMouseButton.new()
	release.button_index = MOUSE_BUTTON_LEFT
	release.pressed = false
	Input.parse_input_event(release)
	arena.start_round("clicking", {"seed": 8, "duration_s": 5.0})
	arena.stop_round()
	assert(not finished.completed)
	print("PASS abort, geometry, replay, complete, seeds, telemetry")
	if "--export-traces" in OS.get_cmdline_user_args():
		Engine.max_fps = 240
		DirAccess.make_dir_recursive_absolute("res://build")
		for drill in ["clicking", "switching", "tracking"]:
			await export_trace(drill)
	if "--snapshot" in OS.get_cmdline_user_args() and DisplayServer.get_name() != "headless":
		DisplayServer.window_set_size(Vector2i(1920, 1080))
		root.size = Vector2i(1920, 1080)
		arena.start_round("switching", {"seed": 734, "duration_s": 5.0})
		arena._running = false
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
		await process_frame
		await process_frame
		await RenderingServer.frame_post_draw
		DirAccess.make_dir_recursive_absolute("res://build")
		var result: int = root.get_texture().get_image().save_png("res://build/arena.png")
		assert(result == OK)
		print("Saved build/arena.png")
	if "--profile" in OS.get_cmdline_user_args():
		Engine.max_fps = 240
		arena.start_round("tracking", {"seed": 734, "duration_s": 5.0})
		while arena._running:
			await process_frame
		var times: Array[float] = []
		for sample in finished.samples:
			if float(sample.frame_ms) > 0.0:
				times.append(float(sample.frame_ms))
		times.sort()
		assert(times.size() > 50)
		var mean := 0.0
		for value in times:
			mean += value
		mean /= times.size()
		print("PROFILE render_texture=%s window=%s duration=%.3fs completed=%s mean_ms=%.3f p95_ms=%.3f sample_count=%d" % [root.get_texture().get_size(), root.size, finished.duration_s, finished.completed, mean, times[int((times.size() - 1) * 0.95)], times.size()])
	arena.queue_free()
	await process_frame
	await process_frame
	quit(0)


func export_trace(drill: String) -> void:
	arena.start_round(drill, {"seed": 734, "duration_s": 5.0, "sound_enabled": false})
	var target_id := ""
	var trial_start := 0.0
	var trial_origin := Vector2.ZERO
	while arena._running:
		await process_frame
		if not arena._running:
			break
		var target: Dictionary = arena._current_target()
		if target.id != target_id:
			target_id = target.id
			trial_start = arena._elapsed
			trial_origin = Vector2(arena._yaw, arena._pitch)
		var desired: Vector2
		var center := Vector2(float(target.yaw), float(target.pitch))
		var phase: float = (arena._elapsed - trial_start) / 0.6
		if drill == "tracking":
			desired = center + Vector2(cos(arena._elapsed * 4.0) * 0.8, sin(arena._elapsed * 2.0) * 0.6)
			arena._held = true
		else:
			var beyond: Vector2 = center + (center - trial_origin).normalized() * float(target.radius) * 1.7
			desired = trial_origin.lerp(beyond, smoothstep(0.0, 0.55, phase)) if phase < 0.55 else beyond.lerp(center, smoothstep(0.55, 1.0, phase))
		if DisplayServer.get_name() == "headless":
			# The headless display cannot capture a pointer; exercise the same aim state directly.
			arena._yaw = desired.x
			arena._pitch = desired.y
			arena._apply_camera()
		else:
			var motion := InputEventMouseMotion.new()
			motion.screen_relative = Vector2(desired.x - arena._yaw, arena._pitch - desired.y) / float(arena._settings.sensitivity_deg_per_count)
			arena._input(motion)
		if phase >= 1.0 and drill != "tracking":
			if DisplayServer.get_name() == "headless":
				arena._fire_shot()
			else:
				var shot := InputEventMouseButton.new()
				shot.button_index = MOUSE_BUTTON_LEFT
				shot.pressed = true
				arena._input(shot)
				shot.pressed = false
				arena._input(shot)
	assert(finished.completed, "Trace interrupted, possibly by window focus loss")
	assert(finished.samples.size() > 400)
	assert(drill == "tracking" or finished.shots.size() >= 5)
	finished["source"] = "synthetic_smoke"
	var path := "res://build/smoke_%s.json" % drill
	var output := FileAccess.open(path, FileAccess.WRITE)
	output.store_string(JSON.stringify(finished))
	output.close()
	print("TRACE %s samples=%d shots=%d duration=%.3fs" % [path, finished.samples.size(), finished.shots.size(), finished.duration_s])
