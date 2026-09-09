extends SceneTree
## Driven by test_native_music_http.py against an isolated real local companion.

func _initialize() -> void:
	call_deferred("run")
	create_timer(30.0).timeout.connect(func(): quit(1))

func run() -> void:
	assert(DisplayServer.get_name() == "headless")
	assert(AudioServer.get_driver_name() == "Dummy")
	var scene = load("res://game/main.tscn").instantiate()
	root.add_child(scene)
	assert(scene.voice._microphone == null and not scene.voice.is_active)
	var ready := FileAccess.open(OS.get_environment("AIMCOACH_TEST_READY"),FileAccess.WRITE)
	assert(ready != null)
	ready.store_string(JSON.stringify({"settings_path":ProjectSettings.globalize_path("user://settings.json")}))
	ready.close()
	while not FileAccess.file_exists(OS.get_environment("AIMCOACH_TEST_STOP")):
		await create_timer(0.02).timeout
		assert(scene.voice._microphone == null and not scene.voice.is_active,"Native music and volume tools must never open the microphone")
	root.remove_child(scene)
	scene.queue_free()
	await create_timer(0.2).timeout
	print("NATIVE_MUSIC_HTTP_PASS")
	quit()
