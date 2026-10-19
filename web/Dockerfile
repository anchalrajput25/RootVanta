# The dashboard as a small Node server. Requests to /api/v1 are passed on to the API.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG INCIDENT_AGENT_API_URL=http://api:8000
ENV INCIDENT_AGENT_API_URL=$INCIDENT_AGENT_API_URL NEXT_STANDALONE=1 NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3001 HOSTNAME=0.0.0.0 NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER node
EXPOSE 3001
CMD ["node", "server.js"]
