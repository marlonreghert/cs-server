package util

import (
	"cs-server/models"
	"fmt"
	"log"
	"os"

	"github.com/go-echarts/go-echarts/v2/charts"
	"github.com/go-echarts/go-echarts/v2/opts"
	"github.com/go-echarts/go-echarts/v2/types"
)

// PlotBoundingBox generates an HTML file rendering the bounding box of a venue.
func PlotBoundingBox(response models.SearchVenuesResponse) {
	// Extract the coordinates from the response's bounding box.
	latMin := response.BoundingBox.LatMin
	latMax := response.BoundingBox.LatMax
	lngMin := response.BoundingBox.LngMin
	lngMax := response.BoundingBox.LngMax

	// Define the points forming the bounding box polygon.
	points := []opts.GeoData{
		{Name: "SW", Value: []float64{lngMin, latMin}},
		{Name: "NW", Value: []float64{lngMin, latMax}},
		{Name: "NE", Value: []float64{lngMax, latMax}},
		{Name: "SE", Value: []float64{lngMax, latMin}},
		{Name: "SW", Value: []float64{lngMin, latMin}}, // Close the polygon.
	}

	// Create a new Geo chart.
	geo := charts.NewGeo()
	geo.SetGlobalOptions(
		charts.WithInitializationOpts(opts.Initialization{
			PageTitle: "Bounding Box Map",
			Width:     "800px",
			Height:    "600px",
		}),
		charts.WithGeoComponentOpts(opts.GeoComponent{
			Map:    "world",         // Select appropriate map (e.g., "world" or custom map).
			Silent: opts.Bool(true), // Disables interactivity on the map background.
		}),
	)

	// Add a scatter series with the bounding box points.
	geo.AddSeries("BoundingBox", types.ChartScatter, points,
		charts.WithLabelOpts(opts.Label{
			Show:      opts.Bool(true),
			Formatter: "{b}",
		}),
		charts.WithLabelOpts(opts.Label{
			Show:      opts.Bool(true),
			Formatter: "{b}",
		}),
	)

	// Create an HTML file to render the chart.
	f, err := os.Create("bounding_box_map.html")
	if err != nil {
		log.Fatalf("Failed to create HTML file: %v", err)
	}
	defer f.Close()

	// Render the chart into the HTML file.
	if err := geo.Render(f); err != nil {
		log.Fatalf("Failed to render chart: %v", err)
	}

	fmt.Println("Bounding box map generated: bounding_box_map.html")
}
