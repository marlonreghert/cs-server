package models

import "net/url"
import "strconv"

// VenueFilterParams mirrors the API’s query args. Use zero-values to omit.
type VenueFilterParams struct {
	CollectionID  string   // optional
	BusyMin       *int     // optional
	BusyMax       *int     // optional
	BusyConf      string   // "any" (default) | "all"
	FootTraffic   string   // "limited"(default) | "day" | "both"
	HourMin       *int     // optional (0..24)
	HourMax       *int     // optional (0..24)
	DayInt        *int     // optional (0..6)
	Now           *bool    // optional
	Live          *bool    // optional
	Types         []string // e.g. []{"BAR","CAFE","RESTAURANT"}
	Lat           *float64 // optional; must be paired with Lng & Radius
	Lng           *float64 // optional
	Radius        *int     // optional (meters)
	LatMin        *float64 // optional; bounding-box set must be complete
	LngMin        *float64
	LatMax        *float64
	LngMax        *float64
	PriceMin      *int
	PriceMax      *int
	RatingMin     *float64 // 2.0, 2.5, 3.0, 3.5, 4.0, 4.5
	RatingMax     *float64 // 2.0 .. 5.0
	ReviewsMin    *int
	ReviewsMax    *int
	DayRankMin    *int
	DayRankMax    *int
	OwnVenuesOnly *bool    // default depends on account age; explicit if you want
	OrderBy       string   // e.g. "day_rank_max,reviews"
	Order         string   // e.g. "desc,desc"
	Limit         *int     // default 5000
	Page          *int     // default 0
}

func (p VenueFilterParams) ToValues() url.Values {
	q := url.Values{}

	if p.CollectionID != "" {
		q.Set("collection_id", p.CollectionID)
	}
	if p.BusyMin != nil {
		q.Set("busy_min", itoa(*p.BusyMin))
	}
	if p.BusyMax != nil {
		q.Set("busy_max", itoa(*p.BusyMax))
	}
	if p.BusyConf != "" {
		q.Set("busy_conf", p.BusyConf)
	}
	if p.FootTraffic != "" {
		q.Set("foot_traffic", p.FootTraffic)
	}
	if p.HourMin != nil {
		q.Set("hour_min", itoa(*p.HourMin))
	}
	if p.HourMax != nil {
		q.Set("hour_max", itoa(*p.HourMax))
	}
	if p.DayInt != nil {
		q.Set("day_int", itoa(*p.DayInt))
	}
	if p.Now != nil {
		q.Set("now", btoa(*p.Now))
	}
	if p.Live != nil {
		q.Set("live", btoa(*p.Live))
	}
	// NOTE: Types parameter is omitted to increase response accuracy per BestTime API recommendations
	if len(p.Types) > 0 {
		// API expects comma-separated list
		q.Set("types", join(p.Types, ","))
	}
	if p.Lat != nil {
		q.Set("lat", ftoa(*p.Lat))
	}
	if p.Lng != nil {
		q.Set("lng", ftoa(*p.Lng))
	}
	if p.Radius != nil {
		q.Set("radius", itoa(*p.Radius))
	}
	if p.LatMin != nil { q.Set("lat_min", ftoa(*p.LatMin)) }
	if p.LngMin != nil { q.Set("lng_min", ftoa(*p.LngMin)) }
	if p.LatMax != nil { q.Set("lat_max", ftoa(*p.LatMax)) }
	if p.LngMax != nil { q.Set("lng_max", ftoa(*p.LngMax)) }

	if p.PriceMin != nil { q.Set("price_min", itoa(*p.PriceMin)) }
	if p.PriceMax != nil { q.Set("price_max", itoa(*p.PriceMax)) }
	if p.RatingMin != nil { q.Set("rating_min", ftoa(*p.RatingMin)) }
	if p.RatingMax != nil { q.Set("rating_max", ftoa(*p.RatingMax)) }
	if p.ReviewsMin != nil { q.Set("reviews_min", itoa(*p.ReviewsMin)) }
	if p.ReviewsMax != nil { q.Set("reviews_max", itoa(*p.ReviewsMax)) }
	if p.DayRankMin != nil { q.Set("day_rank_min", itoa(*p.DayRankMin)) }
	if p.DayRankMax != nil { q.Set("day_rank_max", itoa(*p.DayRankMax)) }
	if p.OwnVenuesOnly != nil { q.Set("own_venues_only", btoa(*p.OwnVenuesOnly)) }

	if p.OrderBy != "" { q.Set("order_by", p.OrderBy) }
	if p.Order != "" { q.Set("order", p.Order) }

	if p.Limit != nil { q.Set("limit", itoa(*p.Limit)) }
	if p.Page  != nil { q.Set("page",  itoa(*p.Page)) }

	return q
}

// lightweight helpers (no fmt.Sprintf allocations for ints/bools)
func itoa(i int) string        { return strconv.Itoa(i) }
func ftoa(f float64) string    { return strconv.FormatFloat(f, 'f', -1, 64) }
func btoa(b bool) string       { if b { return "true" }; return "false" }
func join(ss []string, sep string) string {
	if len(ss) == 0 { return "" }
	out := ss[0]
	for i := 1; i < len(ss); i++ { out += sep + ss[i] }
	return out
}
