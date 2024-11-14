# Use an official Go base image
FROM golang:1.23-alpine

# Set the working directory inside the container
WORKDIR /app

# Copy go.mod and go.sum first
COPY go.mod go.sum ./

# Download dependencies (cached if go.mod/go.sum haven't changed)
RUN go mod download

# Copy the rest of the application code
COPY . .

# Build the application
RUN go build -o cs-server .

# Expose the necessary port
EXPOSE 8080

# Run the application
CMD ["./cs-server"]
