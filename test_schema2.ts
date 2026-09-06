import { z } from "npm:zod";
import zodToJsonSchema from "npm:zod-to-json-schema";
const schema = z.object({
  action: z.enum([
    "DISPLAY_STOCK",
    "DISPLAY_WATCHLIST",
  ]).describe("The action to perform"),
});
console.log(JSON.stringify(zodToJsonSchema(schema, "mySchema"), null, 2));
