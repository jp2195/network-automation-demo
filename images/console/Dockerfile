# Scenario console (wave-2 feature 5). Multi-stage: build the static Go
# binary, ship it on distroless/static — no shell, no package manager,
# ~10-20 MB. The page + console-targets.json are //go:embed-ed into the
# binary, so the final image is just the binary. Build context = repo root.
FROM golang:1.26 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /console ./tools/console

FROM gcr.io/distroless/static:nonroot
COPY --from=build /console /console
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/console"]
