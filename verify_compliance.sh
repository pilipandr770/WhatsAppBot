#!/bin/bash
# Verification Script for Meta Compliance Fixes

echo "🔍 VERIFYING META COMPLIANCE FIXES..."
echo "=========================================="

# Count color changes
echo ""
echo "1️⃣  Checking color #25D366 (WhatsApp Green) - should be 0 or minimal:"
grep -r "#25D366" app/templates/ 2>/dev/null | wc -l
echo "   Expected: 0 (or only in comments)"

echo ""
echo "2️⃣  Checking new color #6366F1 (Indigo) - should be high:"
grep -r "#6366F1" app/templates/ 2>/dev/null | wc -l
echo "   Expected: 20+ occurrences"

echo ""
echo "3️⃣  Checking 'WhatsApp Bot Helfer' mentions - should be 0:"
grep -r "WhatsApp Bot Helfer" app/templates/ 2>/dev/null | wc -l
echo "   Expected: 0"

echo ""
echo "4️⃣  Checking 'AI Chat Pro' mentions - should be high:"
grep -r "AI Chat Pro" app/templates/ 2>/dev/null | wc -l
echo "   Expected: 20+ occurrences"

echo ""
echo "5️⃣  Checking disclaimers added:"
echo "   Landing page footer:"
grep -c "AI Chat Pro is an independent service" app/templates/landing.html && echo "   ✅ Found" || echo "   ❌ Missing"

echo "   Login page:"
grep -c "not affiliated with Meta" app/templates/auth/login.html && echo "   ✅ Found" || echo "   ❌ Missing"

echo "   Register page:"
grep -c "not affiliated with Meta" app/templates/auth/register.html && echo "   ✅ Found" || echo "   ❌ Missing"

echo "   Billing page:"
grep -c "not affiliated with Meta" app/templates/billing/plans.html && echo "   ✅ Found" || echo "   ❌ Missing"

echo ""
echo "=========================================="
echo "✅ Verification complete!"
echo ""
echo "📝 Next steps:"
echo "1. Deploy changes: git push origin main"
echo "2. Verify on production"
echo "3. Submit Appeal to Meta"
echo ""
