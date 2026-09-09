extends Node3D
## A stationary, raw-input aim arena. Angular telemetry uses right/up-positive degrees.

signal round_finished(record: Dictionary)
signal hud_updated(info: Dictionary)
signal replay_finished
signal pause_requested

const EYE := Vector3(0.0, 2.3, 0.0)
const TARGET_DISTANCE := 22.0
const SAMPLE_INTERVAL := 1.0 / 120.0
const DRILL_VERSION := 1
const REACTIVE_INTERVAL := 0.75
const REACTIVE_MAX_STEP := 13.0
const REACTIVE_BOUNDS := Vector2(24.0, 10.0)

var camera: Camera3D
var _target_root: Node3D
var _active_material: StandardMaterial3D
var _inactive_material: StandardMaterial3D
var _engaged_material: StandardMaterial3D
var _targets: Array[Dictionary] = []
var _active_index := 0
var _target_serial := 0
var _rng := RandomNumberGenerator.new()
var _settings: Dictionary = {}
var _record: Dictionary = {}
var _drill := "clicking"
var _running := false
var paused := false
var _just_resumed := false
var _pause_started_usec := 0
var _replaying := false
var _held := false
var _yaw := 0.0
var _pitch := 0.0
var _elapsed := 0.0
var _duration := 45.0
var _start_usec := 0
var _sample_budget := 0.0
var _hud_budget := 0.0
var _on_target_time := 0.0
var _engaged_time := 0.0
var _hits := 0
var _shot_count := 0
var _flash := 0.0
var _tracking_phase := 0.0
var _reactive_points := PackedVector2Array()
var _replay_record: Dictionary = {}
var _replay_sample_index := 0
var _replay_event_index := 0
var _replay_shot_index := 0
var _hit_sound: AudioStreamPlayer
var _replay_trail: MeshInstance3D


func _ready() -> void:
	Input.use_accumulated_input = false
	_build_arena()
	_build_hit_sound()
	set_process(true)


func start_round(drill: String, settings: Dictionary, prescription: Dictionary = {}) -> void:
	_running = false
	paused = false
	_just_resumed = false
	_pause_started_usec = 0
	stop_replay()
	_clear_targets()
	visible = true
	_drill = drill if drill in ["clicking", "tracking", "switching"] else "clicking"
	_settings = settings.duplicate(true)
	for key in ["target_scale", "speed_scale", "duration_s"]:
		if prescription.has(key):
			_settings[key] = prescription[key]
	_settings["fov"] = clampf(float(_settings.get("fov", 103.0)), 60.0, 130.0)
	_settings["sensitivity_deg_per_count"] = clampf(float(_settings.get("sensitivity_deg_per_count", 0.07)), 0.001, 1.0)
	_settings["target_scale"] = clampf(float(_settings.get("target_scale", 1.0)), 0.5, 2.0)
	_settings["speed_scale"] = clampf(float(_settings.get("speed_scale", 1.0)), 0.4, 2.0)
	_settings["seed"] = int(_settings.get("seed", Time.get_ticks_usec()))
	_settings["fov_axis"] = "horizontal"
	_settings["drill_version"] = DRILL_VERSION
	_duration = clampf(float(_settings.get("duration_s", 45.0)), 5.0, 180.0)
	_settings["duration_s"] = _duration
	_rng.seed = _settings["seed"]
	_tracking_phase = _rng.randf_range(0.0, TAU)
	_reactive_points.clear()
	if _drill == "tracking" and _settings.get("tracking_motion", "smooth") == "reactive":
		_build_reactive_path()
	_yaw = 0.0
	_pitch = 0.0
	_elapsed = 0.0
	_sample_budget = 0.0
	_hud_budget = 0.0
	_on_target_time = 0.0
	_engaged_time = 0.0
	_hits = 0
	_shot_count = 0
	_flash = 0.0
	# A player may already be holding fire after reading the countdown instruction.
	_held = Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)
	_target_serial = 0
	_active_index = 0
	_record = {
		"schema_version": 1,
		"id": "%x-%x-%x" % [int(Time.get_unix_time_from_system()), Time.get_ticks_usec(), randi()],
		"drill": _drill,
		"started_at": int(Time.get_unix_time_from_system()),
		"duration_s": 0.0,
		"completed": false,
		"pause_count": 0, "paused_seconds": 0.0,
		"settings": _settings.duplicate(true),
		"samples": [], "shots": [], "events": []
	}
	camera.fov = float(_settings["fov"])
	_apply_camera()
	if _drill == "switching":
		for slot in range(3):
			_targets.append(_make_target(slot))
	else:
		_targets.append(_make_target(0))
	if _drill == "tracking":
		_move_tracking_target()
	_refresh_target_materials(false)
	_log_event("spawn", _current_target(), true)
	_start_usec = Time.get_ticks_usec()
	_running = true
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	_capture_sample(0.0)
	_emit_hud()


func stop_round() -> void:
	if _running:
		if not paused:
			_elapsed = minf(float(Time.get_ticks_usec() - _start_usec) / 1000000.0, _duration)
		_finish_round(false)


func pause_round() -> void:
	if not _running or paused:
		return
	# Account for active time up to this input edge before freezing the arena.
	_process(0.0)
	if not _running:
		return
	_capture_sample(0.0)
	paused = true
	_pause_started_usec = Time.get_ticks_usec()
	_record.pause_count += 1
	_held = false
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	_emit_hud()


func resume_round() -> void:
	if not _running or not paused:
		return
	# Rebase the clock so time spent in the menu never enters aim measurements.
	_record_pause_duration()
	_start_usec = Time.get_ticks_usec() - int(round(_elapsed * 1000000.0))
	paused = false
	_just_resumed = true
	_sample_budget = 0.0
	_hud_budget = 0.0
	_held = Input.is_mouse_button_pressed(MOUSE_BUTTON_LEFT)
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	_capture_sample(0.0)
	_emit_hud()


func _record_pause_duration() -> void:
	if _pause_started_usec == 0:
		return
	_record.paused_seconds += float(Time.get_ticks_usec() - _pause_started_usec) / 1000000.0
	_pause_started_usec = 0


func set_active(value: bool) -> void:
	if not value:
		stop_round()
		stop_replay()
	visible = value


func start_replay(record: Dictionary) -> void:
	_running = false
	paused = false
	stop_replay()
	_clear_targets()
	_replay_record = record
	if record.get("samples", []).is_empty():
		replay_finished.emit()
		return
	visible = true
	_drill = str(record.get("drill", "clicking"))
	_settings = record.get("settings", {}).duplicate(true)
	_duration = float(record.get("duration_s", 0.0))
	camera.fov = float(_settings.get("fov", 103.0))
	_elapsed = 0.0
	_hud_budget = 0.0
	_replay_sample_index = 0
	_replay_event_index = 0
	_replay_shot_index = 0
	_hits = 0
	_shot_count = 0
	_on_target_time = 0.0
	_held = false
	_replaying = true
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	_advance_replay(0.0)


func stop_replay() -> void:
	_replaying = false
	_replay_record = {}
	if is_instance_valid(_replay_trail):
		_replay_trail.visible = false
	if not _running:
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE


func _input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and not event.echo and event.keycode == KEY_ESCAPE:
		if _running and not paused:
			pause_round()
			if paused: pause_requested.emit()
			get_viewport().set_input_as_handled()
		elif _replaying:
			stop_replay()
			replay_finished.emit()
			get_viewport().set_input_as_handled()
		return
	if not _running or paused or Input.mouse_mode != Input.MOUSE_MODE_CAPTURED:
		return
	if event is InputEventMouseMotion:
		var sensitivity := float(_settings["sensitivity_deg_per_count"])
		_yaw = wrapf(_yaw + event.screen_relative.x * sensitivity, -180.0, 180.0)
		_pitch = clampf(_pitch - event.screen_relative.y * sensitivity, -80.0, 80.0)
		_apply_camera()
		get_viewport().set_input_as_handled()
	elif event is InputEventMouseButton and event.button_index == MOUSE_BUTTON_LEFT:
		_held = event.pressed
		if event.pressed and _drill != "tracking":
			_fire_shot()
		get_viewport().set_input_as_handled()


func _notification(what: int) -> void:
	if what == NOTIFICATION_APPLICATION_FOCUS_OUT and _running and not paused:
		pause_round()
		if paused: pause_requested.emit()


func _process(delta: float) -> void:
	if paused:
		return
	_flash = maxf(0.0, _flash - delta)
	if _replaying:
		_advance_replay(delta)
		return
	if not _running:
		return
	var previous_elapsed := _elapsed
	_elapsed = minf(float(Time.get_ticks_usec() - _start_usec) / 1000000.0, _duration)
	var scored_delta := maxf(0.0, _elapsed - previous_elapsed)
	if _just_resumed:
		delta = scored_delta
		_just_resumed = false
	if _drill == "tracking":
		_move_tracking_target()
	var target := _current_target()
	var on_target := _angular_error(target) <= float(target.get("radius", 0.0))
	var engaged := _held if _drill == "tracking" else true
	if engaged:
		_engaged_time += scored_delta
		if on_target:
			_on_target_time += scored_delta
	_refresh_target_materials(on_target and engaged)
	_sample_budget += delta
	if _sample_budget >= SAMPLE_INTERVAL:
		_sample_budget = fmod(_sample_budget, SAMPLE_INTERVAL)
		_capture_sample(delta * 1000.0)
	_hud_budget += delta
	if _hud_budget >= 0.05:
		_hud_budget = 0.0
		_emit_hud()
	if _elapsed >= _duration:
		_finish_round(true)


func _fire_shot() -> void:
	if not _running or paused:
		return
	_elapsed = minf(float(Time.get_ticks_usec() - _start_usec) / 1000000.0, _duration)
	if _elapsed >= _duration:
		_finish_round(true)
		return
	var target := _current_target()
	var error := _angular_error(target)
	var hit := error <= float(target.get("radius", 0.0))
	_shot_count += 1
	var shot := {"t": _elapsed, "hit": hit, "target_id": target.get("id", ""), "error_deg": error}
	if not target.is_empty():
		# Capture the click itself: periodic samples cannot establish its exact position.
		shot.merge({"aim_yaw": _yaw, "aim_pitch": _pitch, "target_yaw": target["yaw"],
			"target_pitch": target["pitch"], "target_radius": target["radius"]})
	_record["shots"].append(shot)
	# Event-edge samples preserve the exact final correction before the target changes.
	_capture_sample(0.0)
	if hit:
		_hits += 1
		_flash = 0.10
		if bool(_settings.get("sound_enabled", true)):
			_hit_sound.play()
		_log_event("hit", target)
		_log_event("despawn", target)
		var old_mesh: MeshInstance3D = target["mesh"]
		old_mesh.visible = false
		old_mesh.queue_free()
		_targets[_active_index] = _make_target(_active_index)
		if _drill == "switching":
			_active_index = (_active_index + 1) % 3
		_refresh_target_materials(false)
		_log_event("spawn", _current_target(), true)
		_capture_sample(0.0)
	_emit_hud()


func _capture_sample(frame_ms: float) -> void:
	var target := _current_target()
	if target.is_empty():
		return
	_record["samples"].append({
		"t": _elapsed, "yaw": _yaw, "pitch": _pitch,
		"target_id": target["id"], "target_yaw": target["yaw"],
		"target_pitch": target["pitch"], "target_radius": target["radius"],
		"on_target": _angular_error(target) <= float(target["radius"]),
		"engaged": _held if _drill == "tracking" else true,
		"frame_ms": frame_ms
	})


func _finish_round(completed: bool) -> void:
	if not _running:
		return
	_record_pause_duration()
	_capture_sample(0.0)
	_running = false
	paused = false
	_just_resumed = false
	_held = false
	_record["duration_s"] = _elapsed
	_record["completed"] = completed
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	_emit_hud()
	round_finished.emit(_record)


func _make_target(slot: int) -> Dictionary:
	_target_serial += 1
	var radius := 0.95
	var angles := Vector2.ZERO
	if _drill == "tracking":
		radius = 1.55
	elif _drill == "switching":
		radius = 1.25
		angles = Vector2(float(slot - 1) * 21.0 + _rng.randf_range(-5.0, 5.0), _rng.randf_range(-10.0, 13.0))
	else:
		# Reject tiny jumps so each target presents a useful acquisition rather than a double click.
		for attempt in range(12):
			angles = Vector2(_rng.randf_range(-22.0, 22.0), _rng.randf_range(-11.0, 13.0))
			if angles.distance_to(Vector2(_yaw, _pitch)) >= 5.0:
				break
	var target := {"id": "t%04d" % _target_serial, "yaw": angles.x, "pitch": angles.y,
		"radius": radius * float(_settings.get("target_scale", 1.0))}
	_attach_target_mesh(target)
	return target


func _attach_target_mesh(target: Dictionary) -> void:
	var sphere := SphereMesh.new()
	var radius := TARGET_DISTANCE * sin(deg_to_rad(float(target["radius"])))
	sphere.radius = radius
	sphere.height = radius * 2.0
	sphere.radial_segments = 32
	sphere.rings = 16
	var mesh := MeshInstance3D.new()
	mesh.mesh = sphere
	mesh.material_override = _active_material
	mesh.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_target_root.add_child(mesh)
	target["mesh"] = mesh
	_position_target(target)


func _position_target(target: Dictionary) -> void:
	var mesh: MeshInstance3D = target["mesh"]
	mesh.position = EYE + _direction(float(target["yaw"]), float(target["pitch"])) * TARGET_DISTANCE


func _move_tracking_target() -> void:
	if _targets.is_empty():
		return
	var t := _elapsed * float(_settings.get("speed_scale", 1.0))
	var target := _targets[0]
	if not _reactive_points.is_empty():
		var angles := _reactive_position(t)
		target["yaw"] = angles.x
		target["pitch"] = angles.y
	else:
		# The original smooth benchmark remains unchanged.
		target["yaw"] = 21.0 * sin(t * 0.78 + _tracking_phase) + 4.0 * sin(t * 1.73 + _tracking_phase * 0.5)
		target["pitch"] = 8.0 * sin(t * 0.91 + _tracking_phase * 0.8) + 3.0 * sin(t * 1.57)
	_position_target(target)


func _build_reactive_path() -> void:
	var path_rng := RandomNumberGenerator.new()
	path_rng.seed = int(_settings.seed) ^ 0x5A17C9
	var point := Vector2(path_rng.randf_range(-12.0, 12.0), path_rng.randf_range(-5.0, 5.0))
	var count := int(ceil(_duration * float(_settings.speed_scale) / REACTIVE_INTERVAL)) + 4
	for index in range(count):
		_reactive_points.append(point)
		var direction := path_rng.randf_range(0.0, TAU)
		point += Vector2(cos(direction), sin(direction)) * path_rng.randf_range(7.0, REACTIVE_MAX_STEP)
		# Reflection keeps every waypoint inside the arena without abrupt clamps.
		for axis in [0, 1]:
			var bound: float = REACTIVE_BOUNDS[axis]
			if point[axis] > bound: point[axis] = 2.0 * bound - point[axis]
			elif point[axis] < -bound: point[axis] = -2.0 * bound - point[axis]


func _reactive_position(t: float) -> Vector2:
	# A uniform cubic B-spline stays inside its waypoint bounds and is C2 continuous.
	# Consecutive waypoints are at most 13 degrees apart, bounding acceleration by
	# 2*13/0.75^2 degrees/s^2 before the chosen speed multiplier is applied.
	var segment := minf(maxf(t, 0.0) / REACTIVE_INTERVAL, float(_reactive_points.size() - 3) - 0.000001)
	var index := int(floor(segment))
	var u := segment - float(index)
	var u2 := u * u
	var u3 := u2 * u
	var one_minus := 1.0 - u
	return (_reactive_points[index] * (one_minus * one_minus * one_minus)
		+ _reactive_points[index + 1] * (3.0 * u3 - 6.0 * u2 + 4.0)
		+ _reactive_points[index + 2] * (-3.0 * u3 + 3.0 * u2 + 3.0 * u + 1.0)
		+ _reactive_points[index + 3] * u3) / 6.0


func _current_target() -> Dictionary:
	return _targets[_active_index] if not _targets.is_empty() else {}


func _direction(yaw_deg: float, pitch_deg: float) -> Vector3:
	var yaw_rad := deg_to_rad(yaw_deg)
	var pitch_rad := deg_to_rad(pitch_deg)
	return Vector3(sin(yaw_rad) * cos(pitch_rad), sin(pitch_rad), -cos(yaw_rad) * cos(pitch_rad))


func _angular_error(target: Dictionary) -> float:
	if target.is_empty():
		return 180.0
	return rad_to_deg(_direction(_yaw, _pitch).angle_to(_direction(float(target["yaw"]), float(target["pitch"]))))


func _apply_camera() -> void:
	camera.rotation = Vector3(deg_to_rad(_pitch), deg_to_rad(-_yaw), 0.0)


func _refresh_target_materials(engaged_on_target: bool) -> void:
	for i in range(_targets.size()):
		var mesh: MeshInstance3D = _targets[i]["mesh"]
		mesh.material_override = (_engaged_material if engaged_on_target else _active_material) if i == _active_index else _inactive_material


func _log_event(kind: String, target: Dictionary, snapshot: bool = false) -> void:
	var event := {"t": _elapsed, "type": kind, "target_id": target["id"],
		"yaw": target["yaw"], "pitch": target["pitch"], "radius": target["radius"]}
	if snapshot:
		var visual_targets: Array = []
		for item in _targets:
			visual_targets.append({"id": item["id"], "yaw": item["yaw"], "pitch": item["pitch"], "radius": item["radius"]})
		event["targets_snapshot"] = visual_targets
	_record["events"].append(event)


func _emit_hud() -> void:
	var target := _current_target()
	var on_target := not target.is_empty() and _angular_error(target) <= float(target.get("radius", 0.0))
	hud_updated.emit({
		"drill": _drill, "elapsed_s": _elapsed, "remaining_s": maxf(0.0, _duration - _elapsed),
		"hits": _hits, "shots": _shot_count,
		"accuracy": float(_hits) / float(_shot_count) * 100.0 if _shot_count else 0.0,
		"tracking_pct": _on_target_time / maxf(_elapsed, 0.001) * 100.0,
		"score": int(_on_target_time * 100.0) if _drill == "tracking" else _hits * 100,
		"on_target": on_target, "engaged": _held, "hit_flash": _flash > 0.0,
		"paused": paused, "replay": _replaying
	})


func _clear_targets() -> void:
	for target in _targets:
		var mesh: MeshInstance3D = target.get("mesh")
		if is_instance_valid(mesh):
			mesh.visible = false
			mesh.queue_free()
	_targets.clear()
	_active_index = 0


func _advance_replay(delta: float) -> void:
	_elapsed = minf(_elapsed + delta, _duration)
	var events: Array = _replay_record.get("events", [])
	while _replay_event_index < events.size() and float(events[_replay_event_index].get("t", 0.0)) <= _elapsed:
		var event: Dictionary = events[_replay_event_index]
		if event.get("type") == "spawn":
			_clear_targets()
			var snapshots: Array = event.get("targets_snapshot", [{"id": event["target_id"], "yaw": event["yaw"], "pitch": event["pitch"], "radius": event["radius"]}])
			for source in snapshots:
				var target: Dictionary = source.duplicate()
				_attach_target_mesh(target)
				_targets.append(target)
				if target["id"] == event["target_id"]:
					_active_index = _targets.size() - 1
		elif event.get("type") == "hit":
			_flash = 0.10
		_replay_event_index += 1
	var samples: Array = _replay_record.get("samples", [])
	while _replay_sample_index + 1 < samples.size() and float(samples[_replay_sample_index + 1]["t"]) <= _elapsed:
		_replay_sample_index += 1
	var current: Dictionary = samples[_replay_sample_index]
	var following: Dictionary = samples[mini(_replay_sample_index + 1, samples.size() - 1)]
	var fraction := clampf((_elapsed - float(current["t"])) / maxf(0.00001, float(following["t"]) - float(current["t"])), 0.0, 1.0)
	_yaw = rad_to_deg(lerp_angle(deg_to_rad(float(current["yaw"])), deg_to_rad(float(following["yaw"])), fraction))
	_pitch = lerpf(float(current["pitch"]), float(following["pitch"]), fraction)
	_held = bool(current.get("engaged", false))
	_apply_camera()
	if _drill == "tracking" and not _targets.is_empty():
		var target := _targets[0]
		target["yaw"] = lerpf(float(current["target_yaw"]), float(following["target_yaw"]), fraction)
		target["pitch"] = lerpf(float(current["target_pitch"]), float(following["target_pitch"]), fraction)
		_position_target(target)
	if bool(current.get("on_target", false)) and _held:
		_on_target_time += delta
	_refresh_target_materials(bool(current.get("on_target", false)) and _held)
	var shots: Array = _replay_record.get("shots", [])
	while _replay_shot_index < shots.size() and float(shots[_replay_shot_index]["t"]) <= _elapsed:
		_shot_count += 1
		if bool(shots[_replay_shot_index].get("hit", false)):
			_hits += 1
		_replay_shot_index += 1
	_hud_budget += delta
	if _hud_budget >= 0.05 or delta == 0.0:
		_hud_budget = 0.0
		_update_replay_trail(samples)
		_emit_hud()
	if _elapsed >= _duration:
		_replaying = false
		replay_finished.emit()


func _update_replay_trail(samples: Array) -> void:
	var start_index := _replay_sample_index
	while start_index > 0 and float(samples[start_index - 1]["t"]) >= _elapsed - 0.75:
		start_index -= 1
	if _replay_sample_index - start_index < 2:
		_replay_trail.visible = false
		return
	var lines := ImmediateMesh.new()
	lines.surface_begin(Mesh.PRIMITIVE_LINE_STRIP)
	for i in range(start_index, _replay_sample_index + 1, 2):
		var sample: Dictionary = samples[i]
		lines.surface_add_vertex(EYE + _direction(float(sample["yaw"]), float(sample["pitch"])) * (TARGET_DISTANCE - 1.0))
	lines.surface_add_vertex(EYE + _direction(_yaw, _pitch) * (TARGET_DISTANCE - 1.0))
	lines.surface_end()
	_replay_trail.mesh = lines
	_replay_trail.visible = true


func _material(color: Color, unshaded: bool = false, emission: float = 0.0) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = color
	material.roughness = 0.85
	if unshaded:
		material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	if emission > 0.0:
		material.emission_enabled = true
		material.emission = color
		material.emission_energy_multiplier = emission
	return material


func _box(size: Vector3, at: Vector3, material: Material) -> void:
	var mesh := MeshInstance3D.new()
	var box := BoxMesh.new()
	box.size = size
	mesh.mesh = box
	mesh.position = at
	mesh.material_override = material
	mesh.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	add_child(mesh)


func _build_arena() -> void:
	camera = Camera3D.new()
	camera.name = "AimCamera"
	camera.position = EYE
	camera.keep_aspect = Camera3D.KEEP_WIDTH
	camera.fov = 103.0
	camera.near = 0.05
	camera.far = 150.0
	add_child(camera)
	camera.make_current()
	var environment := WorldEnvironment.new()
	var world := Environment.new()
	world.background_mode = Environment.BG_COLOR
	world.background_color = Color("080d19")
	world.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	world.ambient_light_color = Color("8594ba")
	world.ambient_light_energy = 0.65
	world.tonemap_mode = Environment.TONE_MAPPER_FILMIC
	environment.environment = world
	add_child(environment)
	var key := DirectionalLight3D.new()
	key.rotation_degrees = Vector3(-35.0, -25.0, 0.0)
	key.light_color = Color("ccd6ed")
	key.light_energy = 1.2
	key.shadow_enabled = false
	add_child(key)
	var floor_mat := _material(Color("111a2b"))
	var wall_mat := _material(Color("111c30"))
	var grid_mat := _material(Color("203650"), true)
	var cyan_mat := _material(Color("237c91"), true)
	var purple_mat := _material(Color("594e86"), true)
	_box(Vector3(90, 0.3, 85), Vector3(0, -4.0, -25.0), floor_mat)
	_box(Vector3(86, 42, 0.3), Vector3(0, 10, -37), wall_mat)
	_box(Vector3(0.3, 42, 65), Vector3(-43, 10, -14), wall_mat)
	_box(Vector3(0.3, 42, 65), Vector3(43, 10, -14), wall_mat)
	for x in range(-40, 41, 5):
		_box(Vector3(0.018, 0.014, 75), Vector3(x, -3.84, -26), grid_mat)
		_box(Vector3(0.018, 34, 0.015), Vector3(x, 10, -36.82), grid_mat)
	for z in range(-60, 11, 5):
		_box(Vector3(80, 0.014, 0.018), Vector3(0, -3.84, z), grid_mat)
	for y in range(-3, 29, 5):
		_box(Vector3(80, 0.018, 0.015), Vector3(0, y, -36.82), grid_mat)
	_box(Vector3(64, 0.04, 0.08), Vector3(0, -3.7, -35), cyan_mat)
	_box(Vector3(0.06, 19, 0.08), Vector3(-32, 5.8, -35), cyan_mat)
	_box(Vector3(0.06, 19, 0.08), Vector3(32, 5.8, -35), cyan_mat)
	_box(Vector3(64, 0.04, 0.08), Vector3(0, 15.3, -35), purple_mat)
	_active_material = _material(Color("62f7dc"), false, 0.45)
	_engaged_material = _material(Color("ceffef"), false, 0.6)
	_inactive_material = _material(Color("4d5681"), false, 0.1)
	_target_root = Node3D.new()
	_target_root.name = "Targets"
	add_child(_target_root)
	_replay_trail = MeshInstance3D.new()
	_replay_trail.name = "ReplayAimTrail"
	var trail_material := _material(Color("ffcf83"), true)
	trail_material.no_depth_test = true
	_replay_trail.material_override = trail_material
	_replay_trail.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	_replay_trail.visible = false
	add_child(_replay_trail)


func _build_hit_sound() -> void:
	# Tiny procedural confirmation; no external assets or audio thread work while aiming.
	var rate := 22050
	var count := int(float(rate) * 0.055)
	var pcm := PackedByteArray()
	pcm.resize(count * 2)
	for i in range(count):
		var t := float(i) / float(rate)
		var envelope := pow(1.0 - float(i) / float(count), 3.0)
		var value := int((sin(TAU * 980.0 * t) * 0.7 + sin(TAU * 1470.0 * t) * 0.3) * envelope * 8000.0)
		pcm.encode_s16(i * 2, value)
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = rate
	stream.data = pcm
	_hit_sound = AudioStreamPlayer.new()
	_hit_sound.stream = stream
	_hit_sound.volume_db = -10.0
	add_child(_hit_sound)
