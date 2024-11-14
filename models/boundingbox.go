package models

type BoundingBox struct {
	Lat     float64 `json:"lat"`
	LatMax  float64 `json:"lat_max"`
	LatMin  float64 `json:"lat_min"`
	Lng     float64 `json:"lng"`
	LngMax  float64 `json:"lng_max"`
	LngMin  float64 `json:"lng_min"`
	MapZoom int     `json:"map_zoom"`
	Radius  int     `json:"radius"`
}
