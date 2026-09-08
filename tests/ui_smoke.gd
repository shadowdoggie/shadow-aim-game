extends SceneTree

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var scene = load("res://game/main.tscn").instantiate()
	root.add_child(scene)
	await create_timer(0.8).timeout
	DirAccess.make_dir_recursive_absolute("res://build")
	if DisplayServer.get_name() != "headless":
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png("res://build/home.png")
	scene._settings_page()
	await create_timer(0.2).timeout
	if DisplayServer.get_name() != "headless":
		await RenderingServer.frame_post_draw
		root.get_texture().get_image().save_png("res://build/settings.png")
	scene._home()
	await create_timer(0.2).timeout
	if "--results" in OS.get_cmdline_user_args():
		var preview = JSON.parse_string(FileAccess.get_file_as_string("res://build/ui_preview.json"))
		scene.last_record = preview.record
		scene.last_report = preview.report
		scene.recommendation = preview.coaching
		scene._results()
		await create_timer(0.2).timeout
		if DisplayServer.get_name() != "headless":
			await RenderingServer.frame_post_draw
			root.get_texture().get_image().save_png("res://build/coaching.png")
	scene._begin_round("tracking",scene.settings.duplicate(true))
	await create_timer(0.1).timeout
	scene._notification(Node.NOTIFICATION_APPLICATION_FOCUS_OUT)
	await create_timer(2).timeout
	assert(scene.is_playing and scene.paused and scene.pause_menu.active and not scene.arena._running,"Unfocused countdown must pause without starting gameplay")
	scene.pause_menu.resume()
	await create_timer(2.1).timeout
	assert(scene.is_playing and not scene.paused and scene.arena._running,"Resume restarts the interrupted countdown")
	scene._pause_game()
	assert(scene.arena.paused and scene.pause_menu.active,"Gameplay pause opens the menu")
	scene.pause_menu.end_round()
	assert(not scene.is_playing and not scene.paused and scene.screen == "home","End round returns from pause")
	print("UI_SMOKE_PASS")
	root.remove_child(scene)
	scene.queue_free()
	await process_frame
	quit()
