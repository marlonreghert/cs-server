package besttime

import (
    "fmt"
    "log"
    "net/url"

    "cs-server/api"
    "cs-server/models"
    "cs-server/models/venue"
	"cs-server/models/live_forecast"
)

// BestTimeApiClient embeds HTTPClient and holds both keys.
type BestTimeApiClient struct {
    *api.HTTPClient
    apiKeyPublic  string
    apiKeyPrivate string
}

// NewBestTimeApiClient creates a new instance; keys start empty.
func NewBestTimeApiClient(httpClient *api.HTTPClient) *BestTimeApiClient {
    return &BestTimeApiClient{
        HTTPClient:    httpClient,
        apiKeyPublic:  "",
        apiKeyPrivate: "",
    }
}

// SetCredentials sets both API credentials.
func (c *BestTimeApiClient) SetCredentials(apiKeyPublic, apiKeyPrivate string) {
    c.apiKeyPublic = apiKeyPublic
    c.apiKeyPrivate = apiKeyPrivate
}

// callWithPublicKey injects "api_key_public" into the JSON body, with logging.
func (c *BestTimeApiClient) callWithPublicKey(
    method, path string,
    params map[string]string,
    body map[string]interface{},
    out interface{},
) error {
    if body == nil {
        body = make(map[string]interface{})
    }
    body["api_key_public"] = c.apiKeyPublic

    log.Printf("[BestTimeApiClient] Calling %s %s params=%v body=%v",
        method, path, params, body)
    err := c.Request(method, path, params, body, out)
    if err != nil {
        log.Printf("[BestTimeApiClient] Error on %s %s: %v", method, path, err)
    } else {
        log.Printf("[BestTimeApiClient] Success on %s %s response=%#v", method, path, out)
    }
    return err
}

// callWithPrivateKey injects "api_key_private" into the JSON body, with logging.
func (c *BestTimeApiClient) callWithPrivateKey(
    method, path string,
    params map[string]string,
    body map[string]interface{},
    out interface{},
) error {
    if body == nil {
        body = make(map[string]interface{})
    }
    body["api_key_private"] = c.apiKeyPrivate

    log.Printf("[BestTimeApiClient] Calling %s %s params=%v body=%v",
        method, path, params, body)
    err := c.Request(method, path, params, body, out)
    if err != nil {
        log.Printf("[BestTimeApiClient] Error on %s %s: %v", method, path, err)
    } else {
        log.Printf("[BestTimeApiClient] Success on %s %s response=%#v", method, path, out)
    }
    return err
}

// GetVenuesNearby kicks off the background search & returns the job-handle,
// now using callWithPrivateKey to inject the private key.
func (c *BestTimeApiClient) GetVenuesNearby(lat, lng float64) (*models.SearchVenuesResponse, error) {
    // Build query parameters into the endpoint URL
    q := url.Values{}
    q.Set("api_key_private", c.apiKeyPrivate)
    q.Set("q", "most popular bars, nightclubs or pubs to party and dance in recife and are open now")
    q.Set("num", "20")
    q.Set("lat", fmt.Sprintf("%v", lat))
    q.Set("lng", fmt.Sprintf("%v", lng))
    q.Set("opened", "now")
    q.Set("radius", "10000")
    q.Set("live", "true")
    endpoint := "/venues/search?" + q.Encode()

    var resp models.SearchVenuesResponse
    // wrap the call so we get logging and key injection in JSON body too
    if err := c.callWithPrivateKey("POST", endpoint, nil, nil, &resp); err != nil {
        return nil, err
    }
    return &resp, nil
}

// GetVenueSearchProgress polls the background job; no key-wrapper used here.
func (c *BestTimeApiClient) GetVenueSearchProgress(jobID, collectionID string) (*models.SearchProgressResponse, error) {
    q := url.Values{}
    q.Set("job_id", jobID)
    if collectionID != "" {
        q.Set("collection_id", collectionID)
    }
    endpoint := "/venues/progress?" + q.Encode()

    var resp models.SearchProgressResponse
    if err := c.Request("GET", endpoint, nil, nil, &resp); err != nil {
        return nil, err
    }
    return &resp, nil
}

// GetVenue wraps GET /venues/{id} and uses the public key.
func (c *BestTimeApiClient) GetVenue(venueId string) (*venue.Venue, error) {
    var resp venue.Venue
    if err := c.callWithPublicKey("GET", "/venues/"+venueId, nil, nil, &resp); err != nil {
        return nil, err
    }
    return &resp, nil
}



// GetLiveForecast retrieves live busyness by venue_id or (venue_name + venue_address),
// placing every parameter (including api_key_private) in the query string.
func (c *BestTimeApiClient) GetLiveForecast(
    venueID, venueName, venueAddress string,
) (*live_forecast.LiveForecastResponse, error) {
    // Build query params
    q := url.Values{}
    q.Set("api_key_private", c.apiKeyPrivate)

    if venueID != "" {
        q.Set("venue_id", venueID)
    } else {
        if venueName == "" || venueAddress == "" {
            return nil, fmt.Errorf(
                "either venueID or both venueName and venueAddress must be provided",
            )
        }
        q.Set("venue_name", venueName)
        q.Set("venue_address", venueAddress)
    }

    endpoint := "/forecasts/live?" + q.Encode()

    var resp live_forecast.LiveForecastResponse
    // No JSON body, all inputs live in the URL
    if err := c.Request("POST", endpoint, nil, nil, &resp); err != nil {
        return nil, err
    }
    return &resp, nil
}

// VenueFilter calls GET /venues/filter with api_key_private and given filters in the query string.
func (c *BestTimeApiClient) VenueFilter(params models.VenueFilterParams) (*models.VenueFilterResponse, error) {
    q := params.ToValues()
    // API requires the private key in the querystring
    q.Set("api_key_private", c.apiKeyPrivate)

    endpoint := "/venues/filter?" + q.Encode()
    log.Printf("[BestTimeApiClient] Calling GET %s", endpoint)

    var resp models.VenueFilterResponse
    if err := c.Request("GET", endpoint, nil, nil, &resp); err != nil {
        log.Printf("[BestTimeApiClient] Error on GET %s: %v", endpoint, err)
        return nil, err
    }

    log.Printf("[BestTimeApiClient] Success GET %s; status=%s venues_n=%d", endpoint, resp.Status, resp.VenuesN)
    return &resp, nil
}