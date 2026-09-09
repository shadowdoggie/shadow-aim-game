extends Node
## A balanced, blinded sensitivity comparison. Trial settings never replace the profile.

const LIME = Color("c5f878")
const TEXT = Color("edf3e9")
const MUTED = Color("98aaa8")
const TITLES = {"clicking":"Precision clicking", "tracking":"Smooth tracking"}
var host: Node
var active := false
var progress_text := ""
var experiment: Dictionary = {}
var analysis: Dictionary = {}
var review: Dictionary = {}
var original_settings: Dictionary = {}
var pending_record: Dictionary = {}
var labels: Dictionary = {}
var warming := false
var busy := false
var generation := 0
var review_job := ""
var poll_elapsed := 0.0
var polling := false
var message := ""
var review_started_ms := 0
var review_status: Label
var review_stale := false

func setup(owner_node: Node) -> void:
	host = owner_node

func show_intro() -> void:
	if host.is_playing or host.is_replaying: return
	if active:
		_show_next()
		return
	if not analysis.is_empty():
		_show_results()
		return
	var body := _page()
	host._label(body, "FIND YOUR SENSITIVITY", 14, LIME)
	host._label(body, "Let your aim decide.", 42)
	host._paragraph(body, "Try three nearby settings in short clicking and tracking rounds. Your coach checks which one helps you stay accurate and move comfortably.", 21)
	var card: VBoxContainer = host._card(body)
	host._label(card, "12 short rounds · about 5–6 minutes", 24)
	host._paragraph(card, "5 seconds of unscored warmup → targets reset → 20 seconds scored. Take a breather between rounds whenever you need one.")
	host._paragraph(card, "The settings are labelled A, B and C while you play so the numbers do not influence you. Keep your mouse DPI unchanged.")
	host._paragraph(card, "This finds a promising setting to try; it cannot establish your perfect sensitivity in one session.", 16)
	if not message.is_empty(): host._paragraph(card, message, 17, LIME)
	host._button(card, "Start sensitivity finder  →", start, true)
	host._paragraph(card, "Your saved sensitivity only changes if you choose to apply the result.", 15)
	host._button(body, "Back to training", host._home)
	_saved_result_link(card)

func _saved_result_link(parent: VBoxContainer) -> void:
	var page_id: int = host.page_generation
	var response: Dictionary = await host._api("/sensitivity")
	if page_id != host.page_generation or not is_instance_valid(parent): return
	for saved in response.get("experiments",[]):
		if saved.get("status") == "complete":
			var id: String = str(saved.id)
			host._button(parent,"View your last completed comparison",func(): load_saved(id))
			return

func load_saved(id: String) -> void:
	if host.is_playing or host.is_replaying: return
	cancel()
	analysis = {}
	review = {}
	message = ""
	var current := generation
	var body := _page()
	var page_id: int = host.page_generation
	host._label(body,"Loading your saved comparison…",32)
	var response: Dictionary = await host._api("/sensitivity/"+id.uri_encode())
	if current != generation or page_id != host.page_generation: return
	if not response.get("experiment") is Dictionary:
		message = "Could not load your saved comparison: " + str(response.get("error","Please retry."))
		show_intro()
		return
	_restore_result(response)
	_show_results()
	if not review.is_empty() and host.has_method("_voice_context"):
		host._voice_context(false,{"sensitivity_review":review,"analysis":analysis})

func _restore_result(response: Dictionary) -> void:
	review_stale = bool(response.get("review_stale",false))
	if response.get("experiment") is Dictionary: experiment = response.experiment
	original_settings = experiment.get("base_settings",host.settings).duplicate(true)
	if response.get("analysis") is Dictionary: analysis = response.analysis
	elif response.get("report") is Dictionary: analysis = response.report
	review = experiment.get("review",{}) if experiment.get("review") is Dictionary else {}
	labels = {}
	for block in experiment.get("blocks",[]):
		var id: String = str(block.candidate_id)
		if not labels.has(id): labels[id] = ["A","B","C"][labels.size() % 3]

func start() -> void:
	if busy or host.is_playing or host.is_replaying: return
	generation += 1
	var current := generation
	active = true
	busy = true
	experiment = {}
	analysis = {}
	review = {}
	pending_record = {}
	labels = {}
	original_settings = host.settings.duplicate(true)
	host.guided = false
	host.pending_rounds.clear()
	host.stage = "sensitivity"
	message = ""
	var body := _page()
	host._label(body, "Preparing your sensitivity test…", 32)
	var response: Dictionary = await host._api("/sensitivity/start", HTTPClient.METHOD_POST, {"settings":original_settings})
	if current != generation: return
	busy = false
	var created: Dictionary = response.get("experiment",response)
	if not created.has("id") or created.get("blocks",[]).is_empty():
		active = false
		message = "Could not start: " + str(response.get("error","The companion returned an incomplete test."))
		show_intro()
		return
	experiment = created
	for block in experiment.blocks:
		var id: String = str(block.candidate_id)
		if not labels.has(id): labels[id] = ["A","B","C"][labels.size() % 3]
	_show_next()

func _page() -> VBoxContainer:
	host.screen = "sensitivity"
	return host._clear_page()

func _block_index() -> int:
	return experiment.get("record_ids",[]).size()

func _block() -> Dictionary:
	var blocks: Array = experiment.get("blocks",[])
	var index := _block_index()
	return blocks[index] if index < blocks.size() else {}

func _show_next() -> void:
	if not active: return
	var block := _block()
	if block.is_empty():
		_finish_test()
		return
	var body := _page()
	var index := _block_index()
	var total: int = experiment.blocks.size()
	host._label(body, "SENSITIVITY FINDER  ·  %d OF %d COMPLETE" % [index,total], 14, LIME)
	host._label(body, "Ready for the next short round?" if index > 0 else "Get a feel for it.", 38)
	var bar := ProgressBar.new()
	bar.max_value = total
	bar.value = index
	bar.show_percentage = false
	bar.custom_minimum_size.y = 12
	body.add_child(bar)
	var card: VBoxContainer = host._card(body)
	var label: String = str(labels.get(str(block.candidate_id),"?"))
	host._label(card, "Round %d/%d · Setting %s · %s" % [index+1,total,label,TITLES.get(str(block.drill),str(block.drill))], 25)
	host._paragraph(card, "Hold left mouse and follow the moving target." if block.drill == "tracking" else "Click each bright target with a fresh press.", 21, TEXT)
	host._paragraph(card, "5 seconds warmup, then targets reset and the 20-second score starts. The warmup does not count.")
	if not message.is_empty(): host._paragraph(card,message,17,LIME)
	if not pending_record.is_empty():
		host._button(card,"Retry saving this round",_submit_round,true)
	else:
		host._button(card,"Start round %d  →" % [index+1],_begin_block,true)
	host._button(body,"Stop test · keep current sensitivity",func():
		cancel()
		host._home())

func _begin_block() -> void:
	if busy or not active or host.is_playing: return
	var block := _block()
	if block.is_empty(): return
	warming = true
	message = ""
	var config: Dictionary = block.settings.duplicate(true)
	config.duration_s = 5.0
	# Adaptation uses a different sequence; the measured sequence remains matched.
	config.seed = int(config.get("seed",0)) + 7919
	progress_text = "Sensitivity %d/%d · Setting %s · 5s warmup, then 20s scored" % [_block_index()+1,experiment.blocks.size(),labels.get(str(block.candidate_id),"?")]
	host.stage = "sensitivity"
	host._begin_round(str(block.drill),config)

func on_round_finished(record: Dictionary) -> void:
	if not active: return
	host.is_playing = false
	host.crosshair.hide()
	host.hud.hide()
	if not record.get("completed",false):
		cancel()
		message = "Test stopped. Your saved sensitivity is unchanged. Start again when you are ready."
		show_intro()
		return
	var block := _block()
	if block.is_empty(): return
	if warming:
		warming = false
		progress_text = "Sensitivity %d/%d · Setting %s · SCORING" % [_block_index()+1,experiment.blocks.size(),labels.get(str(block.candidate_id),"?")]
		host.is_playing = true
		host.crosshair.show()
		host.hud.show()
		# The user is already aiming: begin measurement directly, with no second countdown.
		host.current_drill = str(block.drill)
		host.current_round_settings = block.settings.duplicate(true)
		host.arena.start_round(str(block.drill),block.settings.duplicate(true))
		if host.has_method("_voice_context"): host._voice_context(false)
		return
	record["sensitivity_context"] = {"experiment_id":experiment.id,"candidate_id":block.candidate_id,"block_index":block.index}
	record["training_context"] = {"kind":"sensitivity","cycle_id":experiment.id}
	pending_record = record.duplicate(true)
	await _submit_round()

func hud_text(info: Dictionary) -> String:
	var block := _block()
	var remaining := maxf(0.0,float(info.get("remaining_s",0.0)))
	var header := "Sensitivity %d/%d · Setting %s" % [_block_index()+1,experiment.get("blocks",[]).size(),labels.get(str(block.get("candidate_id","")),"?")]
	var text: String
	if warming:
		text = "%s\nWARMUP · NOT SCORED\nScoring begins in %ds · targets reset then" % [header,int(ceil(remaining))]
	else:
		var duration := float(block.get("settings",{}).get("duration_s",20.0))
		var phase := "SCORING STARTED" if remaining > duration - 2.0 else "SCORING"
		var score := "%0.1f%% on target" % float(info.get("tracking_pct",0)) if str(info.get("drill","")) == "tracking" else "%d hits · %0.0f%% accuracy" % [int(info.get("hits",0)),float(info.get("accuracy",0))]
		text = "%s\n%s · %ds left\n%s" % [header,phase,int(ceil(remaining)),score]
	if str(info.get("drill","")) == "tracking": text += "\nHold left mouse while tracking"
	return text + "\nEscape to pause · %d FPS" % Engine.get_frames_per_second()

func _submit_round() -> void:
	if busy or pending_record.is_empty() or not active: return
	busy = true
	var current := generation
	var body := _page()
	host._label(body,"Round complete.",38)
	host._paragraph(body,"Saving this measurement…",20)
	var response: Dictionary = await host._api("/sensitivity/"+str(experiment.id)+"/round",HTTPClient.METHOD_POST,{"record":pending_record,"block_index":_block_index()})
	if current != generation: return
	busy = false
	if not response.has("experiment"):
		message = "Could not save: " + str(response.get("error","Please retry.")) + " Your round is kept here for retry."
		push_warning("Sensitivity round save failed (%s/%d): %s" % [str(experiment.id),_block_index(),str(response.get("error","Unknown response"))])
		_show_next()
		return
	experiment = response.experiment
	if response.get("analysis") is Dictionary: analysis = response.analysis
	pending_record = {}
	message = ""
	_show_next()

func _finish_test() -> void:
	active = false
	warming = false
	progress_text = ""
	var current := generation
	var body := _page()
	host._label(body,"Test complete. Comparing your rounds…",32)
	var response: Dictionary = await host._api("/sensitivity/"+str(experiment.id))
	if current != generation or host.screen != "sensitivity": return
	_restore_result(response)
	if analysis.is_empty():
		message = "Could not load your comparison: " + str(response.get("error","Please retry."))
		_show_results()
		return
	if not review.is_empty():
		message = ""
		_show_results()
	else: await _request_review()

func _request_review() -> void:
	if busy or not review_job.is_empty() or experiment.is_empty(): return
	busy = true
	message = "Your coach is reviewing your completed test…"
	review_started_ms = Time.get_ticks_msec()
	_show_results()
	var current := generation
	var response: Dictionary = await host._api("/sensitivity/"+str(experiment.id)+"/review",HTTPClient.METHOD_POST,{})
	if current != generation: return
	busy = false
	if response.has("job_id"):
		review_job = str(response.job_id)
		poll_elapsed = 2.0
	else:
		message = "Coach unavailable: " + str(response.get("error","Please retry when ready."))
	if host.screen == "sensitivity": _show_results()

func _process(delta: float) -> void:
	if (busy or not review_job.is_empty()) and is_instance_valid(review_status):
		review_status.text = "AI review in progress · %ds · your 12 rounds are saved" % int((Time.get_ticks_msec()-review_started_ms)/1000)
	if review_job.is_empty() or polling: return
	poll_elapsed += delta
	if poll_elapsed < 2.0: return
	poll_elapsed = 0.0
	_poll_review()

func _poll_review() -> void:
	polling = true
	var current := generation
	var id := review_job
	var response: Dictionary = await host._api("/jobs/"+id)
	polling = false
	if current != generation or id != review_job: return
	var status: String = str(response.get("status",""))
	if status == "complete":
		review_job = ""
		var result: Dictionary = response.get("result",{})
		if result.get("analysis") is Dictionary: analysis = result.analysis
		if result.get("review") is Dictionary: review = result.review
		review_stale = false
		message = "" if not review.is_empty() else "The coach returned no review. You can retry."
		if not review.is_empty() and host.has_method("_voice_context"):
			host._voice_context(true,{"sensitivity_review":review,"analysis":analysis})
		if host.screen == "sensitivity": _show_results()
	elif status in ["failed","cancelled"] or response.get("error") != null:
		review_job = ""
		message = "Coach " + (status if not status.is_empty() else "unavailable") + ": " + str(response.get("error","Please retry."))
		if host.screen == "sensitivity": _show_results()

func _candidate(id: String) -> Dictionary:
	for candidate in analysis.get("candidates",[]):
		if str(candidate.get("id","")) == id: return candidate
	return {}

func _suggested() -> Dictionary:
	if review.is_empty() or review_stale: return {}
	var id: String = str(review.get("recommended_candidate_id",analysis.get("recommended_candidate_id","current")))
	if id not in analysis.get("allowed_candidate_ids",[]): return {}
	return _candidate(id)

func _metric(metrics: Dictionary, key: String, suffix: String, digits: int = 1) -> String:
	var value = metrics.get(key)
	if value == null: return "—"
	return ("%0.2f%s" if digits == 2 else "%0.1f%s") % [float(value),suffix]

func _show_results() -> void:
	var body := _page()
	host._label(body,"SENSITIVITY COMPARISON",14,LIME)
	host._label(body,"Your test is complete.",40)
	var coach: VBoxContainer = host._card(body)
	var waiting := busy or not review_job.is_empty()
	review_status = null
	if waiting:
		review_status = host._paragraph(coach,"Your coach is reviewing your 12 saved rounds…",23,TEXT)
		host._paragraph(coach,"You can leave this screen. Your review will be saved with the test.",17)
	elif review_stale:
		host._label(coach,"NEW PRACTICE RESULTS",14,LIME)
		host._paragraph(coach,"Your saved AI review predates these rounds. The newer measurements are included below.",20,TEXT)
	elif not review.is_empty():
		host._label(coach,"Juniper has your comparison.",23)
		host._button(coach,"Hear coaching",func(): host._hear_coaching({"sensitivity_review":review,"analysis":analysis}),true)
	elif not message.is_empty():
		host._paragraph(coach,message,21,TEXT)
	if analysis.is_empty():
		host._button(coach,"Load saved comparison",_finish_test,true)
	var suggested := _suggested()
	if not suggested.is_empty():
		var value: float = float(suggested.get("sensitivity_deg_per_count",original_settings.get("sensitivity_deg_per_count",0.07)))
		var original: float = float(host.settings.get("sensitivity_deg_per_count",0.07))
		if not is_equal_approx(value,original):
			host._button(coach,"Try recommended sensitivity  →",_apply_suggestion,true)
			host._paragraph(coach,"%.4f → %.4f degrees/count. Try this setting in normal practice and check again another day." % [original,value],16)
		else:
			host._paragraph(coach,"This is already your saved sensitivity.",19,LIME)
	if not busy and review_job.is_empty() and (review.is_empty() or review_stale) and not analysis.is_empty():
		host._button(coach,"Update coach with these results" if review_stale else "Request AI review",_request_review,true)
	host._button(coach,"Return to training" if waiting else "Continue with my current setting",host._home)
	for pair in analysis.get("follow_up",{}).get("comparisons",[]):
		var follow: VBoxContainer = host._card(body)
		host._label(follow,"Later practice · " + str(host.TITLES.get(pair.get("drill",""),"Practice")),21)
		var before := _candidate(str(pair.get("before_candidate_id","")))
		var after := _candidate(str(pair.get("after_candidate_id","")))
		host._label(follow,"%.4f → %.4f°/count" % [float(before.get("sensitivity_deg_per_count",0)),float(after.get("sensitivity_deg_per_count",0))],17)
		var names := {"accuracy_pct":"Accuracy","acquisition_ms":"Acquisition","overshoot_pct":"Overshoots"}
		if pair.get("drill") == "tracking": names = {"time_on_target_pct":"Time on target","tracking_error_deg":"Aim error"}
		for key in names:
			var values: Dictionary = pair.get("metrics",{}).get(key,{})
			if not values.is_empty(): host._label(follow,"%s: %.1f → %.1f %s" % [names[key],float(values.previous),float(values.current),str(values.get("unit",""))],17)
	for candidate in analysis.get("candidates",[]):
		var card: VBoxContainer = host._card(body)
		var id: String = str(candidate.get("id",""))
		var title: String = {"current":"Original setting","lower":"Lower sensitivity","higher":"Higher sensitivity"}.get(id,"Tested setting")
		var value: float = float(candidate.get("sensitivity_deg_per_count",0))
		if is_equal_approx(value,float(host.settings.sensitivity_deg_per_count)): title += " · currently saved"
		host._label(card,"Setting %s · %s · %.4f°/count" % [labels.get(id,"?"),title,value],21)
		var metrics: Dictionary = candidate.get("metrics",{})
		host._paragraph(card,"Clicking: %s accuracy · %s acquisition · %s hits/sec" % [_metric(metrics,"accuracy_pct","%"),_metric(metrics,"acquisition_ms"," ms"),_metric(metrics,"click_hits_per_s","",2)],17,TEXT)
		host._paragraph(card,"Tracking: %s on target · %s aim error" % [_metric(metrics,"time_on_target_pct","%"),_metric(metrics,"tracking_error_deg","°",2)],17,TEXT)
	host._paragraph(body,"These rounds are saved. You can reopen this comparison without playing or asking the AI again.",15)
	if not busy and review_job.is_empty(): host._button(body,"Run a fresh comparison",start)

func _apply_suggestion() -> void:
	var candidate := _suggested()
	if candidate.is_empty(): return
	var value: float = float(candidate.get("sensitivity_deg_per_count",0))
	if value < 0.001 or value > 0.5: return
	host.settings.sensitivity_deg_per_count = value
	host._save_settings()
	cancel()
	host._settings_page()

func cancel() -> void:
	generation += 1
	active = false
	warming = false
	busy = false
	progress_text = ""
	pending_record = {}
	# Trial settings are supplied to the arena only; there is nothing to restore.
	if not review_job.is_empty():
		var id := review_job
		review_job = ""
		host._api("/jobs/"+id+"/cancel",HTTPClient.METHOD_POST,{})
