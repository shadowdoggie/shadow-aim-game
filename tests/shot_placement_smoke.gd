extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var arena = load("res://game/arena.gd").new()
	root.add_child(arena)
	await process_frame
	for drill in ["clicking", "switching"]:
		arena.start_round(drill, {"seed": 734, "duration_s": 5.0, "sound_enabled": false})
		var target: Dictionary = arena._current_target()
		var old_id: String = target.id
		var old_yaw: float = target.yaw
		var old_pitch: float = target.pitch
		var old_radius: float = target.radius
		arena._yaw = old_yaw + old_radius * 0.4
		arena._pitch = old_pitch
		arena._fire_shot()
		var shot: Dictionary = arena._record.shots.back()
		assert(shot.hit and shot.target_id == old_id)
		assert(arena._current_target().id != old_id)
		assert(is_equal_approx(shot.aim_yaw, old_yaw + old_radius * 0.4))
		assert(is_equal_approx(shot.aim_pitch, old_pitch))
		assert(is_equal_approx(shot.target_yaw, old_yaw))
		assert(is_equal_approx(shot.target_pitch, old_pitch))
		assert(is_equal_approx(shot.target_radius, old_radius))
		assert(shot.error_deg > 0.0 and shot.error_deg < old_radius)
		arena.stop_round()
	print("PASS exact shot-time placement survives target replacement")
	arena.queue_free()
	await process_frame
	quit(0)
