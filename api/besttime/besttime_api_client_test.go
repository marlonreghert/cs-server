package besttime

import (
    "encoding/json"
    "io/ioutil"
    "net/http"
    "net/http/httptest"
    "testing"

    "cs-server/api"
    "cs-server/models"
    "cs-server/models/venue"
)

func TestGetVenuesNearby(t *testing.T) {
    var received map[string]interface{}
    wantResp := models.SearchVenuesResponse{
        CollectionID: "col-123",
        CountTotal:   7,
    }

    // Handler to verify request and return stubbed JSON
    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        // method + path
        if r.Method != "GET" {
            t.Errorf("expected GET; got %s", r.Method)
        }
        if r.URL.Path != "/venues/search" {
            t.Errorf("expected path /venues/search; got %s", r.URL.Path)
        }

        // read+unmarshal body
        b, _ := ioutil.ReadAll(r.Body)
        json.Unmarshal(b, &received)

        // must include private key
        if got, ok := received["api_key_private"]; !ok || got != "secret" {
            t.Errorf("api_key_private = %v; want secret", got)
        }

        w.Header().Set("Content-Type", "application/json")
        json.NewEncoder(w).Encode(wantResp)
    }))
    defer srv.Close()

    client := NewBestTimeApiClient(api.NewHTTPClient(srv.URL))
    client.SetAPIKeyPrivate("secret")

    got, err := client.GetVenuesNearby(1.23, 4.56)
    if err != nil {
        t.Fatal(err)
    }
    // response unmarshaled correctly
    if got.CollectionID != wantResp.CollectionID {
        t.Errorf("CollectionID = %q; want %q", got.CollectionID, wantResp.CollectionID)
    }
    if got.CountTotal != wantResp.CountTotal {
        t.Errorf("CountTotal = %d; want %d", got.CountTotal, wantResp.CountTotal)
    }

    // verify all forced fields
    checks := []struct {
        key  string
        want interface{}
    }{
        {"q", "bar or event_venue or club"},
        {"num", 5.0},
        {"lat", 1.23},
        {"lng", 4.56},
        {"opened", "now"},
        {"radius", 10000.0},
        {"live", true},
    }
    for _, c := range checks {
        if got, ok := received[c.key]; !ok || got != c.want {
            t.Errorf("body[%q] = %v; want %v", c.key, got, c.want)
        }
    }
}

func TestGetVenue(t *testing.T) {
    var received map[string]interface{}
    wantResp := venue.Venue{} // empty OK

    srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        if r.Method != "GET" {
            t.Errorf("expected GET; got %s", r.Method)
        }
        if r.URL.Path != "/venues/venue-42" {
            t.Errorf("expected /venues/venue-42; got %s", r.URL.Path)
        }
        b, _ := ioutil.ReadAll(r.Body)
        json.Unmarshal(b, &received)

        w.Header().Set("Content-Type", "application/json")
        json.NewEncoder(w).Encode(wantResp)
    }))
    defer srv.Close()

    client := NewBestTimeApiClient(api.NewHTTPClient(srv.URL))
    client.SetAPIKeyPublic("pubkey")

    got, err := client.GetVenue("venue-42")
    if err != nil {
        t.Fatal(err)
    }
    if got == nil {
        t.Fatal("expected non-nil Venue")
    }
    if received["api_key_public"] != "pubkey" {
        t.Errorf("api_key_public = %v; want pubkey", received["api_key_public"])
    }
}