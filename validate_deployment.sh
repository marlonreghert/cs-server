#!/bin/bash
# Deployment validation script for cs-server

set -e

echo "=========================================="
echo "CS-Server Deployment Validation"
echo "=========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
HOST="localhost"
PORT="8080"
BASE_URL="http://${HOST}:${PORT}"

# Test counter
TESTS_PASSED=0
TESTS_FAILED=0

# Helper functions
pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((TESTS_PASSED++))
}

fail() {
    echo -e "${RED}✗${NC} $1"
    ((TESTS_FAILED++))
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# 1. Check if containers are running
echo "1. Checking container status..."
if docker-compose ps | grep -q "cs-server.*Up"; then
    pass "CS-Server container is running"
else
    fail "CS-Server container is not running"
    exit 1
fi

if docker-compose ps | grep -q "redis.*Up"; then
    pass "Redis container is running"
else
    fail "Redis container is not running"
    exit 1
fi
echo ""

# 2. Check health endpoint
echo "2. Testing health endpoint..."
HEALTH_RESPONSE=$(curl -s "${BASE_URL}/health" || echo "FAILED")
if echo "$HEALTH_RESPONSE" | grep -q '"status":"healthy"'; then
    pass "Health endpoint returned healthy status"
else
    fail "Health endpoint check failed: $HEALTH_RESPONSE"
fi
echo ""

# 3. Check ping endpoint
echo "3. Testing ping endpoint..."
PING_RESPONSE=$(curl -s "${BASE_URL}/ping" || echo "FAILED")
if echo "$PING_RESPONSE" | grep -q '"status":"pong"'; then
    pass "Ping endpoint working"
else
    fail "Ping endpoint check failed: $PING_RESPONSE"
fi
echo ""

# 4. Check Redis connection
echo "4. Testing Redis connection..."
if docker-compose logs cs-server | grep -q "Redis connection successful"; then
    pass "Redis connection established"
else
    fail "Redis connection not found in logs"
fi
echo ""

# 5. Check initial data load
echo "5. Verifying initial data load..."
if docker-compose logs cs-server | grep -q "Initial venue refresh completed"; then
    pass "Initial venue refresh completed"
else
    warn "Initial venue refresh not yet completed (may still be running)"
fi

if docker-compose logs cs-server | grep -q "Initial live forecast refresh completed"; then
    pass "Initial live forecast refresh completed"
else
    warn "Initial live forecast refresh not yet completed"
fi

if docker-compose logs cs-server | grep -q "Initial weekly forecast refresh completed"; then
    pass "Initial weekly forecast refresh completed"
else
    warn "Initial weekly forecast refresh not yet completed"
fi
echo ""

# 6. Check scheduler startup
echo "6. Verifying background jobs..."
if docker-compose logs cs-server | grep -q "Background jobs started"; then
    pass "Background jobs started"
else
    fail "Background jobs not started"
fi
echo ""

# 7. Test venues API endpoint
echo "7. Testing venues API..."
VENUES_RESPONSE=$(curl -s "${BASE_URL}/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5&verbose=false" || echo "FAILED")
if echo "$VENUES_RESPONSE" | grep -q '\['; then
    VENUE_COUNT=$(echo "$VENUES_RESPONSE" | grep -o '"venue_id"' | wc -l | tr -d ' ')
    pass "Venues API returned response (${VENUE_COUNT} venues)"
else
    warn "Venues API returned empty or invalid response (data may still be loading)"
fi
echo ""

# 8. Check Redis data
echo "8. Checking Redis data..."
if docker-compose exec -T redis redis-cli KEYS "venues_geo*" | grep -q "venues_geo"; then
    VENUE_COUNT=$(docker-compose exec -T redis redis-cli ZCARD venues_geo_v1 | tr -d '\r')
    pass "Redis contains venue data (${VENUE_COUNT} venues)"
else
    warn "No venue data found in Redis (data may still be loading)"
fi
echo ""

# 9. Check for errors in logs
echo "9. Checking for errors in logs..."
ERROR_COUNT=$(docker-compose logs cs-server | grep -i "error" | grep -v "Error handling" | wc -l | tr -d ' ')
if [ "$ERROR_COUNT" -eq "0" ]; then
    pass "No errors found in logs"
else
    warn "Found ${ERROR_COUNT} errors in logs (review with: docker-compose logs cs-server)"
fi
echo ""

# Summary
echo "=========================================="
echo "Validation Summary"
echo "=========================================="
echo -e "${GREEN}Passed:${NC} ${TESTS_PASSED}"
if [ "$TESTS_FAILED" -gt "0" ]; then
    echo -e "${RED}Failed:${NC} ${TESTS_FAILED}"
else
    echo -e "Failed: ${TESTS_FAILED}"
fi
echo ""

if [ "$TESTS_FAILED" -eq "0" ]; then
    echo -e "${GREEN}✓ Deployment validation successful!${NC}"
    echo ""
    echo "Next steps:"
    echo "  - Monitor logs: docker-compose logs -f cs-server"
    echo "  - Test API: curl \"${BASE_URL}/v1/venues/nearby?lat=-8.07834&lon=-34.90938&radius=5\""
    echo "  - Check scheduler: docker-compose logs cs-server | grep Scheduler"
    exit 0
else
    echo -e "${RED}✗ Deployment validation failed!${NC}"
    echo ""
    echo "Troubleshooting:"
    echo "  - Check logs: docker-compose logs cs-server"
    echo "  - Check Redis: docker-compose logs redis"
    echo "  - Restart services: docker-compose restart"
    exit 1
fi
