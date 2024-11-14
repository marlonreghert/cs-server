// api/http_client.go
package api

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/ioutil"
	"net/http"
	"time"
)

// HTTPClient struct to hold base URL and HTTP client configuration
type HTTPClient struct {
	BaseURL    string
	HTTPClient *http.Client
}

// NewHTTPClient creates a new instance of HTTPClient with default settings
func NewHTTPClient(baseURL string) *HTTPClient {
	return &HTTPClient{
		BaseURL: baseURL,
		HTTPClient: &http.Client{
			Timeout: 10 * time.Second, // Set a timeout for requests
		},
	}
}

// Request makes an HTTP request to the API and decodes the response
func (c *HTTPClient) Request(method, endpoint string, headers map[string]string, body interface{}, response interface{}) error {
	var requestBody []byte
	if body != nil {
		jsonBody, err := json.Marshal(body)
		if err != nil {
			return err
		}
		requestBody = jsonBody
	}

	url := c.BaseURL + endpoint
	req, err := http.NewRequest(method, url, bytes.NewBuffer(requestBody))
	if err != nil {
		return err
	}

	req.Header.Set("Content-Type", "application/json")
	for key, value := range headers {
		req.Header.Set(key, value)
	}

	res, err := c.HTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()

	resBody, err := ioutil.ReadAll(res.Body)
	if err != nil {
		return err
	}

	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return errors.New("unexpected status code: " + res.Status)
	}

	if response != nil {
		return json.Unmarshal(resBody, response)
	}

	return nil
}
