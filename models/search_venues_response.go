package models

type SearchVenuesResponse struct {
	Links          Link          `json:"_links"`
	BoundingBox    BoundingBox   `json:"bounding_box"`
	CollectionID   string        `json:"collection_id"`
	JobID          string        `json:"job_id"`
	Status         string        `json:"status"`
}
