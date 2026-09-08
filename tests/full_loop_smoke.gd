extends SceneTree
## Opt-in live subscription test. Run through launch.py with a temporary data dir.

func _initialize() -> void:
	call_deferred("run")

func check(condition: bool, message: String) -> bool:
	if not condition:
		push_error("FULL_LOOP_FAIL: " + message)
		quit(1)
	return condition

func run() -> void:
	var scene = load("res://game/main.tscn").instantiate()
	root.add_child(scene)
	await create_timer(0.5).timeout
	var raw := FileAccess.get_file_as_string("res://build/smoke_clicking.json")
	if not check(not raw.is_empty(),"Run arena_smoke --export-traces first"): return
	var record: Dictionary = JSON.parse_string(raw)
	record.id = "ui-smoke-" + str(Time.get_ticks_usec())
	scene.stage = "baseline"
	await scene._round_finished(record)
	if not check(not scene.last_report.is_empty(),"Native UI saved real arena telemetry"): return
	print("FULL_LOOP_SAVED")
	var elapsed := 0
	while scene.recommendation.is_empty() and elapsed < 240:
		await create_timer(1).timeout
		elapsed += 1
		if elapsed % 20 == 0: print("FULL_LOOP_WAITING ",elapsed)
	if not check(not scene.recommendation.is_empty(),"Sol/high result reached native UI"): return
	print("FULL_LOOP_COACH_READY ",elapsed,"s")
	if DisplayServer.get_name() != "headless":
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png("res://build/coaching.png")
	scene._replay()
	await create_timer(0.4).timeout
	if not check(scene.is_replaying,"3D replay started from saved record"): return
	scene.arena.stop_replay()
	scene._finish_replay()
	if not check(not scene.is_replaying and scene.screen == "results","Replay returns to results"): return
	var chosen: Dictionary = scene.recommendation.duplicate(true)
	scene._practice_prescription()
	await create_timer(2.1).timeout
	if not check(scene.arena._running,"Prescribed practice started"): return
	if not check(scene.arena._settings.target_scale == chosen.parameters.target_scale,"Coach difficulty was applied"): return
	scene.arena.stop_round()
	scene._retest()
	await create_timer(2.1).timeout
	if not check(scene.arena._running,"Retest started"): return
	if not check(scene.arena._settings.target_scale == record.settings.target_scale,"Retest restored baseline difficulty"): return
	if not check(scene.arena._settings.seed != record.settings.seed,"Retest has a fresh sequence"): return
	scene.arena.stop_round()
	print("FULL_LOOP_PASS: native telemetry, real Sol/high coaching, replay, prescription, retest")
	root.remove_child(scene)
	scene.queue_free()
	await process_frame
	quit()
