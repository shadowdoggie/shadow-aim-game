extends Control

var mode := "clicking"
var samples: Array = []
const GREEN = Color("c5f878")

func _ready() -> void:
	custom_minimum_size = Vector2(0, 104)
	mouse_filter = Control.MOUSE_FILTER_IGNORE

func _draw() -> void:
	var mid := size * 0.5
	if not samples.is_empty():
		var step: int = maxi(1, samples.size() / 350)
		var points := PackedVector2Array()
		for i in range(0, samples.size(), step):
			var sample: Dictionary = samples[i]
			points.append(Vector2(size.x * float(i) / maxf(samples.size()-1, 1), clampf(mid.y + (float(sample.get("yaw", 0)) - float(sample.get("target_yaw", 0))) * 3.0, 4, size.y-4)))
		draw_line(Vector2(0, mid.y), Vector2(size.x, mid.y), Color("46544d"), 1)
		if points.size()>1: draw_polyline(points, GREEN, 1.5, true)
		return
	var locations: Array[Vector2] = [mid + Vector2(-65, 16), mid + Vector2(8,-22), mid + Vector2(76,22)]
	if mode == "tracking":
		var wave := PackedVector2Array()
		for i in range(70):
			wave.append(Vector2(mid.x-110+i*3.2, mid.y+sin(i*.095)*22))
		draw_polyline(wave, Color("435f53"), 2, true)
		locations = [wave[48]]
	if mode == "switching":
		draw_line(locations[0], locations[1], Color("435f53"), 1, true)
		draw_line(locations[1], locations[2], Color("435f53"), 1, true)
	for i in locations.size():
		var p := locations[i]
		draw_circle(p, 18, Color("283a32"))
		draw_arc(p, 18, 0, TAU, 40, GREEN, 1.3, true)
		draw_circle(p, 5 if mode != "tracking" else 8, GREEN)
