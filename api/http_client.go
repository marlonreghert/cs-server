package api

import (
    "bytes"
    "encoding/json"
    "errors"
    "io/ioutil"
    "log"
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
    log.Printf("[HTTPClient] Preparing request %s %s", method, url)
    if body != nil {
        log.Printf("[HTTPClient] Request body: %s", string(requestBody))
    }

    req, err := http.NewRequest(method, url, bytes.NewBuffer(requestBody))
    if err != nil {
        log.Printf("[HTTPClient] Error creating request: %v", err)
        return err
    }

    req.Header.Set("Content-Type", "application/json")
    for key, value := range headers {
        req.Header.Set(key, value)
    }

    log.Printf("[HTTPClient] Sending request with headers: %v", headers)
    res, err := c.HTTPClient.Do(req)
    if err != nil {
        log.Printf("[HTTPClient] Error making HTTP call: %v", err)
        return err
    }
    defer res.Body.Close()

    log.Printf("[HTTPClient] Received response status: %s", res.Status)
    resBody, err := ioutil.ReadAll(res.Body)
    if err != nil {
        log.Printf("[HTTPClient] Error reading response body: %v", err)
        return err
    }
    log.Printf("[HTTPClient] Response body: %s", string(resBody))

    if res.StatusCode < 200 || res.StatusCode >= 300 {
        errMsg := errors.New("unexpected status code: " + res.Status)
        log.Printf("[HTTPClient] %v", errMsg)
        return errMsg
    }

    if response != nil {
        if err := json.Unmarshal(resBody, response); err != nil {
            log.Printf("[HTTPClient] Error unmarshaling response: %v", err)
            return err
        }
        log.Printf("[HTTPClient] Successfully unmarshaled response into %T", response)
    }

    return nil
}
