package com.singlife.junitforge;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParseStart;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.Providers;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Modifier;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.AnnotationDeclaration;
import com.github.javaparser.ast.body.AnnotationMemberDeclaration;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.RecordDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.AssignExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.CastExpr;
import com.github.javaparser.ast.expr.ConditionalExpr;
import com.github.javaparser.ast.expr.EnclosedExpr;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.github.javaparser.ast.expr.LiteralExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.NullLiteralExpr;
import com.github.javaparser.ast.expr.SwitchExpr;
import com.github.javaparser.ast.expr.ThisExpr;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.stmt.CatchClause;
import com.github.javaparser.ast.stmt.DoStmt;
import com.github.javaparser.ast.stmt.ForEachStmt;
import com.github.javaparser.ast.stmt.ForStmt;
import com.github.javaparser.ast.stmt.IfStmt;
import com.github.javaparser.ast.stmt.ReturnStmt;
import com.github.javaparser.ast.stmt.SwitchStmt;
import com.github.javaparser.ast.stmt.Statement;
import com.github.javaparser.ast.stmt.ThrowStmt;
import com.github.javaparser.ast.stmt.WhileStmt;
import com.github.javaparser.ast.type.ClassOrInterfaceType;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Comparator;
import java.util.IdentityHashMap;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * Java 17/21-capable source symbol extractor for the junitforge Python engine.
 *
 * Contract:
 * - Input: one .java source file path.
 * - Output: a single JSON document to stdout.
 * - Errors: diagnostic message to stderr and non-zero exit code.
 *
 * This class intentionally extracts facts only. Test strategy remains in Python.
 */
public final class JavaSymbolExtractor {

    private static final int EXIT_USAGE = 64;
    private static final int EXIT_PARSE = 65;
    private static final int EXIT_IO = 66;
    private static final int EXIT_UNEXPECTED = 70;

    private JavaSymbolExtractor() {
        throw new AssertionError("No instances");
    }

    public static void main(String[] args) {
        if (args.length != 1 || "--help".equals(args[0]) || "-h".equals(args[0])) {
            printUsage();
            System.exit(args.length == 1 ? 0 : EXIT_USAGE);
        }

        Path sourcePath = Paths.get(args[0]).toAbsolutePath().normalize();
        try {
            if (!Files.isRegularFile(sourcePath)) {
                System.err.println("Input is not a readable Java source file: " + sourcePath);
                System.exit(EXIT_IO);
            }

            Map<String, Object> result = parseSourceFile(sourcePath);
            System.out.println(toJson(result));
        } catch (ParseFailureException ex) {
            System.err.println(ex.getMessage());
            System.exit(EXIT_PARSE);
        } catch (IOException ex) {
            System.err.println("I/O error while reading " + sourcePath + ": " + ex.getMessage());
            System.exit(EXIT_IO);
        } catch (Exception ex) {
            System.err.println("Unexpected JavaSymbolExtractor error for " + sourcePath + ": " + ex.getMessage());
            ex.printStackTrace(System.err);
            System.exit(EXIT_UNEXPECTED);
        }
    }

    private static void printUsage() {
        System.err.println("Usage: java -jar javaparser-cli.jar /path/to/SomeClass.java");
    }

    private static Map<String, Object> parseSourceFile(Path sourcePath) throws IOException {
        String source = Files.readString(sourcePath, StandardCharsets.UTF_8);

        ParserConfiguration configuration = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.BLEEDING_EDGE)
                .setAttributeComments(false)
                .setStoreTokens(false);

        JavaParser parser = new JavaParser(configuration);
        ParseResult<CompilationUnit> parseResult = parser.parse(
                ParseStart.COMPILATION_UNIT,
                Providers.provider(source)
        );

        if (!parseResult.isSuccessful() || parseResult.getResult().isEmpty()) {
            String problems = parseResult.getProblems().stream()
                    .map(Object::toString)
                    .collect(Collectors.joining("; "));
            throw new ParseFailureException("JavaParser parse failed for " + sourcePath + ": " + problems);
        }

        CompilationUnit cu = parseResult.getResult().orElseThrow();
        String packageName = cu.getPackageDeclaration()
                .map(pd -> pd.getName().asString())
                .orElse(null);

        List<Map<String, Object>> imports = new ArrayList<>();
        List<String> importNames = new ArrayList<>();
        for (ImportDeclaration imp : cu.getImports()) {
            Map<String, Object> importJson = orderedMap();
            importJson.put("name", imp.getNameAsString());
            importJson.put("static", imp.isStatic());
            importJson.put("asterisk", imp.isAsterisk());
            imports.add(importJson);
            importNames.add(imp.getNameAsString());
        }

        List<Map<String, Object>> types = new ArrayList<>();
        for (TypeDeclaration<?> type : cu.getTypes()) {
            types.add(extractType(type, packageName, null));
        }

        Map<String, Object> primaryType = findPrimaryType(types);

        Map<String, Object> root = orderedMap();
        root.put("schemaVersion", 1);
        root.put("parser", "javaparser-core");
        root.put("sourcePath", sourcePath.toString());
        root.put("packageName", packageName);
        root.put("package", packageName); // Convenience alias for Python-side mapping.
        root.put("imports", imports);
        root.put("importNames", importNames);
        root.put("types", types);
        root.put("primaryType", primaryType);
        root.put("parseOk", true);
        root.put("parseError", null);

        // Convenience mirror of the primary type to simplify Python integration.
        if (primaryType != null) {
            root.put("className", primaryType.get("name"));
            root.put("fqcn", primaryType.get("fqcn"));
            root.put("kind", primaryType.get("kind"));
            root.put("annotations", primaryType.get("annotations"));
            root.put("fields", primaryType.get("fields"));
            root.put("constructors", primaryType.get("constructors"));
            root.put("methods", primaryType.get("methods"));
        }

        return root;
    }

    private static Map<String, Object> findPrimaryType(List<Map<String, Object>> types) {
        if (types.isEmpty()) {
            return null;
        }
        for (Map<String, Object> type : types) {
            Object modifiers = type.get("modifiers");
            if (modifiers instanceof Collection<?> collection && collection.contains("public")) {
                return type;
            }
        }
        return types.get(0);
    }

    private static Map<String, Object> extractType(TypeDeclaration<?> type, String packageName, String enclosingFqcn) {
        String name = type.getNameAsString();
        String fqcn = buildFqcn(packageName, enclosingFqcn, name);
        String kind = typeKind(type);
        Set<String> configurationFieldNames = configurationFieldNames(type);

        Map<String, Object> json = orderedMap();
        json.put("name", name);
        json.put("fqcn", fqcn);
        json.put("kind", kind);
        json.put("modifiers", modifiers(type.getModifiers()));
        json.put("annotations", annotationNames(type.getAnnotations()));
        json.put("annotationExprs", annotationExprs(type.getAnnotations()));
        json.put("extends", firstOrNull(extendedTypes(type)));
        json.put("extendsList", extendedTypes(type));
        json.put("implements", implementedTypes(type));
        json.put("typeParameters", typeParameters(type));
        json.put("fields", extractFields(type, kind));
        json.put("constructors", extractConstructors(type, configurationFieldNames));
        json.put("methods", extractMethods(type, kind, configurationFieldNames));
        json.put("enumConstants", extractEnumConstants(type));
        json.put("recordComponents", extractRecordComponents(type));
        json.put("nested", extractNestedTypes(type, packageName, fqcn));
        json.put("line", nullableStartLine(type));
        json.put("endLine", nullableEndLine(type));
        return json;
    }

    private static String buildFqcn(String packageName, String enclosingFqcn, String name) {
        if (enclosingFqcn != null && !enclosingFqcn.isBlank()) {
            return enclosingFqcn + "." + name;
        }
        if (packageName != null && !packageName.isBlank()) {
            return packageName + "." + name;
        }
        return name;
    }

    private static String typeKind(TypeDeclaration<?> type) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            return cid.isInterface() ? "interface" : "class";
        }
        if (type instanceof EnumDeclaration) {
            return "enum";
        }
        if (type instanceof AnnotationDeclaration) {
            return "annotation";
        }
        if (type instanceof RecordDeclaration) {
            return "record";
        }
        return "type";
    }

    private static List<String> extendedTypes(TypeDeclaration<?> type) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            return cid.getExtendedTypes().stream().map(ClassOrInterfaceType::asString).collect(Collectors.toList());
        }
        return List.of();
    }

    private static List<String> implementedTypes(TypeDeclaration<?> type) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            return cid.getImplementedTypes().stream().map(ClassOrInterfaceType::asString).collect(Collectors.toList());
        }
        if (type instanceof EnumDeclaration ed) {
            return ed.getImplementedTypes().stream().map(ClassOrInterfaceType::asString).collect(Collectors.toList());
        }
        if (type instanceof RecordDeclaration rd) {
            return rd.getImplementedTypes().stream().map(ClassOrInterfaceType::asString).collect(Collectors.toList());
        }
        return List.of();
    }

    private static List<String> typeParameters(TypeDeclaration<?> type) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            return cid.getTypeParameters().stream().map(Object::toString).collect(Collectors.toList());
        }
        if (type instanceof RecordDeclaration rd) {
            return rd.getTypeParameters().stream().map(Object::toString).collect(Collectors.toList());
        }
        return List.of();
    }

    private static Set<String> configurationFieldNames(TypeDeclaration<?> type) {
        Set<String> names = new LinkedHashSet<>();
        for (BodyDeclaration<?> member : membersOf(type)) {
            if (!(member instanceof FieldDeclaration fieldDeclaration)) {
                continue;
            }
            boolean valueField = fieldDeclaration.getAnnotations().stream()
                    .anyMatch(annotation -> "Value".equals(annotation.getName().getIdentifier()));
            if (!valueField) {
                continue;
            }
            for (VariableDeclarator variable : fieldDeclaration.getVariables()) {
                names.add(variable.getNameAsString());
            }
        }
        return names;
    }

    private static Set<String> parameterNames(NodeList<Parameter> parameters) {
        return parameters.stream()
                .map(Parameter::getNameAsString)
                .collect(Collectors.toCollection(LinkedHashSet::new));
    }

    private static List<Map<String, Object>> extractFields(TypeDeclaration<?> type, String ownerKind) {
        List<Map<String, Object>> fields = new ArrayList<>();

        for (BodyDeclaration<?> member : membersOf(type)) {
            if (!(member instanceof FieldDeclaration fieldDeclaration)) {
                continue;
            }
            Set<String> fieldModifiers = modifiers(fieldDeclaration.getModifiers());
            if ("interface".equals(ownerKind)) {
                fieldModifiers.add("public");
                fieldModifiers.add("static");
                fieldModifiers.add("final");
            }

            for (VariableDeclarator variable : fieldDeclaration.getVariables()) {
                Map<String, Object> fieldJson = orderedMap();
                fieldJson.put("name", variable.getNameAsString());
                fieldJson.put("type", variable.getType().asString());
                fieldJson.put("modifiers", fieldModifiers);
                fieldJson.put("annotations", annotationNames(fieldDeclaration.getAnnotations()));
                fieldJson.put("annotationExprs", annotationExprs(fieldDeclaration.getAnnotations()));
                fieldJson.put("initializerPresent", variable.getInitializer().isPresent());
                fieldJson.put("line", startLine(variable).orElse(startLine(fieldDeclaration).orElse(null)));
                fields.add(fieldJson);
            }
        }

        // Treat record components as fields so Lombok/POJO-style Python logic can reuse the same model.
        if (type instanceof RecordDeclaration recordDeclaration) {
            for (Parameter parameter : recordDeclaration.getParameters()) {
                Map<String, Object> fieldJson = orderedMap();
                fieldJson.put("name", parameter.getNameAsString());
                fieldJson.put("type", parameter.getType().asString());
                fieldJson.put("modifiers", Set.of("private", "final"));
                fieldJson.put("annotations", annotationNames(parameter.getAnnotations()));
                fieldJson.put("annotationExprs", annotationExprs(parameter.getAnnotations()));
                fieldJson.put("initializerPresent", false);
                fieldJson.put("line", startLine(parameter).orElse(startLine(recordDeclaration).orElse(null)));
                fields.add(fieldJson);
            }
        }

        return fields;
    }

    private static List<Map<String, Object>> extractConstructors(
            TypeDeclaration<?> type,
            Set<String> configurationFieldNames
    ) {
        List<Map<String, Object>> constructors = new ArrayList<>();
        for (BodyDeclaration<?> member : membersOf(type)) {
            if (member instanceof ConstructorDeclaration constructor) {
                constructors.add(extractConstructor(constructor, configurationFieldNames));
            }
        }

        // Compact canonical constructor for records can be represented as a ConstructorDeclaration by JavaParser.
        // If it is absent, do not synthesize one here; Python should decide how to handle records.
        return constructors;
    }

    private static Map<String, Object> extractConstructor(
            ConstructorDeclaration constructor,
            Set<String> configurationFieldNames
    ) {
        Map<String, Object> json = orderedMap();
        json.put("name", constructor.getNameAsString());
        json.put("modifiers", modifiers(constructor.getModifiers()));
        json.put("returnType", null);
        json.put("params", extractParams(constructor.getParameters()));
        json.put("throws", constructor.getThrownExceptions().stream().map(Object::toString).collect(Collectors.toList()));
        json.put("annotations", annotationNames(constructor.getAnnotations()));
        json.put("annotationExprs", annotationExprs(constructor.getAnnotations()));
        json.put("line", startLine(constructor).orElse(null));
        json.put("endLine", endLine(constructor).orElse(null));
        json.put("isConstructor", true);
        json.put("bodyPresent", true);
        json.put("bodyFacts", safeExtractBodyFacts(
                constructor.getBody(),
                configurationFieldNames,
                parameterNames(constructor.getParameters())
        ));
        return json;
    }

    private static List<Map<String, Object>> extractMethods(
            TypeDeclaration<?> type,
            String ownerKind,
            Set<String> configurationFieldNames
    ) {
        List<Map<String, Object>> methods = new ArrayList<>();
        for (BodyDeclaration<?> member : membersOf(type)) {
            if (member instanceof MethodDeclaration method) {
                methods.add(extractMethod(method, ownerKind, configurationFieldNames));
            } else if (member instanceof AnnotationMemberDeclaration annotationMember) {
                methods.add(extractAnnotationMember(annotationMember));
            }
        }

        if (type instanceof RecordDeclaration recordDeclaration) {
            // Java records generate public component accessor methods named exactly like the component.
            Set<String> existing = methods.stream()
                    .map(m -> String.valueOf(m.get("name")))
                    .collect(Collectors.toCollection(LinkedHashSet::new));
            for (Parameter component : recordDeclaration.getParameters()) {
                if (existing.contains(component.getNameAsString())) {
                    continue;
                }
                Map<String, Object> methodJson = orderedMap();
                methodJson.put("name", component.getNameAsString());
                methodJson.put("modifiers", Set.of("public"));
                methodJson.put("returnType", component.getType().asString());
                methodJson.put("params", List.of());
                methodJson.put("throws", List.of());
                methodJson.put("annotations", List.of("Generated"));
                methodJson.put("annotationExprs", List.of());
                methodJson.put("line", nullableStartLine(recordDeclaration));
                methodJson.put("endLine", nullableEndLine(recordDeclaration));
                methodJson.put("isConstructor", false);
                methodJson.put("bodyPresent", false);
                methodJson.put("generated", true);
                methodJson.put("bodyFacts", emptyBodyFacts());
                methods.add(methodJson);
            }
        }

        return methods;
    }

    private static Map<String, Object> extractMethod(
            MethodDeclaration method,
            String ownerKind,
            Set<String> configurationFieldNames
    ) {
        Set<String> methodModifiers = modifiers(method.getModifiers());
        if (method.isDefault()) {
            methodModifiers.add("default");
        }
        if ("interface".equals(ownerKind) && !methodModifiers.contains("private")) {
            methodModifiers.add("public");
            if (!method.isDefault() && !methodModifiers.contains("static") && method.getBody().isEmpty()) {
                methodModifiers.add("abstract");
            }
        }

        Map<String, Object> json = orderedMap();
        json.put("name", method.getNameAsString());
        json.put("modifiers", methodModifiers);
        json.put("returnType", method.getType().asString());
        json.put("params", extractParams(method.getParameters()));
        json.put("throws", method.getThrownExceptions().stream().map(Object::toString).collect(Collectors.toList()));
        json.put("annotations", annotationNames(method.getAnnotations()));
        json.put("annotationExprs", annotationExprs(method.getAnnotations()));
        json.put("line", startLine(method).orElse(null));
        json.put("endLine", endLine(method).orElse(null));
        json.put("isConstructor", false);
        json.put("bodyPresent", method.getBody().isPresent());
        json.put("generated", false);
        json.put("bodyFacts", method.getBody()
                .map(body -> safeExtractBodyFacts(
                        body,
                        configurationFieldNames,
                        parameterNames(method.getParameters())
                ))
                .orElseGet(JavaSymbolExtractor::emptyBodyFacts));
        return json;
    }

    private static Map<String, Object> extractAnnotationMember(AnnotationMemberDeclaration member) {
        Map<String, Object> json = orderedMap();
        json.put("name", member.getNameAsString());
        json.put("modifiers", Set.of("public", "abstract"));
        json.put("returnType", member.getType().asString());
        json.put("params", List.of());
        json.put("throws", List.of());
        json.put("annotations", annotationNames(member.getAnnotations()));
        json.put("annotationExprs", annotationExprs(member.getAnnotations()));
        json.put("line", startLine(member).orElse(null));
        json.put("endLine", endLine(member).orElse(null));
        json.put("isConstructor", false);
        json.put("bodyPresent", false);
        json.put("generated", false);
        json.put("defaultValuePresent", member.getDefaultValue().isPresent());
        json.put("bodyFacts", emptyBodyFacts());
        return json;
    }

    private static List<Map<String, Object>> extractParams(NodeList<Parameter> parameters) {
        List<Map<String, Object>> params = new ArrayList<>();
        for (Parameter parameter : parameters) {
            Map<String, Object> paramJson = orderedMap();
            paramJson.put("name", parameter.getNameAsString());
            paramJson.put("type", parameter.getType().asString() + (parameter.isVarArgs() ? "..." : ""));
            paramJson.put("varargs", parameter.isVarArgs());
            paramJson.put("annotations", annotationNames(parameter.getAnnotations()));
            paramJson.put("annotationExprs", annotationExprs(parameter.getAnnotations()));
            paramJson.put("line", startLine(parameter).orElse(null));
            params.add(paramJson);
        }
        return params;
    }

    private static List<String> extractEnumConstants(TypeDeclaration<?> type) {
        if (type instanceof EnumDeclaration enumDeclaration) {
            return enumDeclaration.getEntries().stream()
                    .map(entry -> entry.getNameAsString())
                    .collect(Collectors.toList());
        }
        return List.of();
    }

    private static List<Map<String, Object>> extractRecordComponents(TypeDeclaration<?> type) {
        if (!(type instanceof RecordDeclaration recordDeclaration)) {
            return List.of();
        }

        List<Map<String, Object>> components = new ArrayList<>();
        for (Parameter parameter : recordDeclaration.getParameters()) {
            Map<String, Object> component = orderedMap();
            component.put("name", parameter.getNameAsString());
            component.put("type", parameter.getType().asString());
            component.put("annotations", annotationNames(parameter.getAnnotations()));
            component.put("annotationExprs", annotationExprs(parameter.getAnnotations()));
            component.put("line", startLine(parameter).orElse(null));
            components.add(component);
        }
        return components;
    }

    private static List<Map<String, Object>> extractNestedTypes(TypeDeclaration<?> type, String packageName, String enclosingFqcn) {
        List<Map<String, Object>> nested = new ArrayList<>();
        for (BodyDeclaration<?> member : membersOf(type)) {
            if (member instanceof TypeDeclaration<?> nestedType) {
                nested.add(extractType(nestedType, packageName, enclosingFqcn));
            }
        }
        return nested;
    }

    private static List<BodyDeclaration<?>> membersOf(TypeDeclaration<?> type) {
        if (type instanceof ClassOrInterfaceDeclaration cid) {
            return cid.getMembers();
        }
        if (type instanceof EnumDeclaration ed) {
            return ed.getMembers();
        }
        if (type instanceof AnnotationDeclaration ad) {
            return ad.getMembers();
        }
        if (type instanceof RecordDeclaration rd) {
            return rd.getMembers();
        }
        return List.of();
    }

    // ---------------------------------------------------------------------
    // Per-method body facts. These are additive and intentionally fact-only.
    // ---------------------------------------------------------------------

    private static Map<String, Object> emptyBodyFacts() {
        Map<String, Object> facts = orderedMap();
        facts.put("methodCalls", List.of());
        facts.put("branches", List.of());
        facts.put("returnReads", List.of());
        facts.put("memberReads", List.of());
        facts.put("nullGuards", List.of());
        facts.put("exits", List.of());
        facts.put("localVariables", List.of());
        facts.put("assignments", List.of());
        facts.put("configurationReads", List.of());
        facts.put("dereferenceChains", List.of());
        facts.put("lineFacts", List.of());
        return facts;
    }

    private static Map<String, Object> safeExtractBodyFacts(
            BlockStmt body,
            Set<String> configurationFieldNames,
            Set<String> parameterNames
    ) {
        try {
            return extractBodyFacts(body, configurationFieldNames, parameterNames);
        } catch (RuntimeException ex) {
            return emptyBodyFacts();
        }
    }

    private static Map<String, Object> extractBodyFacts(
            BlockStmt body,
            Set<String> configurationFieldNames,
            Set<String> parameterNames
    ) {
        List<BranchDescriptor> branchDescriptors = extractBranchDescriptors(body);
        IdentityHashMap<Node, BranchDescriptor> branchByNode = new IdentityHashMap<>();
        for (BranchDescriptor descriptor : branchDescriptors) {
            branchByNode.put(descriptor.node, descriptor);
        }

        List<Map<String, Object>> methodCalls = new ArrayList<>();
        List<MethodCallExpr> calls = new ArrayList<>(body.findAll(MethodCallExpr.class));
        calls.sort(JavaSymbolExtractor::compareNodes);
        for (MethodCallExpr call : calls) {
            methodCalls.add(extractMethodCall(call, branchByNode));
        }

        Set<String> assignedVariables = methodCalls.stream()
                .map(call -> call.get("assignedTo"))
                .filter(String.class::isInstance)
                .map(String.class::cast)
                .collect(Collectors.toCollection(LinkedHashSet::new));

        List<Map<String, Object>> memberReads = extractMemberReads(body, branchByNode, null);
        List<Map<String, Object>> returnReads = extractMemberReads(body, branchByNode, assignedVariables);
        List<Map<String, Object>> nullGuards = extractNullGuards(body, branchByNode);
        List<Map<String, Object>> exits = extractExits(body, branchByNode);
        List<Map<String, Object>> localVariables = extractLocalVariables(body, branchByNode);
        List<Map<String, Object>> assignments = extractAssignments(body, branchByNode);
        List<Map<String, Object>> configurationReads = extractConfigurationReads(
                body, branchByNode, configurationFieldNames, parameterNames
        );
        List<Map<String, Object>> dereferenceChains = extractDereferenceChains(body, branchByNode);
        List<Map<String, Object>> lineFacts = extractLineFacts(body, branchByNode);

        List<Map<String, Object>> branches = new ArrayList<>();
        for (BranchDescriptor descriptor : branchDescriptors) {
            branches.add(descriptor.toJson(branchByNode));
        }

        Map<String, Object> facts = orderedMap();
        facts.put("methodCalls", methodCalls);
        facts.put("branches", branches);
        facts.put("returnReads", returnReads);
        facts.put("memberReads", memberReads);
        facts.put("nullGuards", nullGuards);
        facts.put("exits", exits);
        facts.put("localVariables", localVariables);
        facts.put("assignments", assignments);
        facts.put("configurationReads", configurationReads);
        facts.put("dereferenceChains", dereferenceChains);
        facts.put("lineFacts", lineFacts);
        return facts;
    }

    private static Map<String, Object> extractMethodCall(
            MethodCallExpr call,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        Map<String, Object> json = orderedMap();
        String scopeName = null;
        String receiverKind;
        String receiverExpr = null;

        Optional<Expression> scope = call.getScope();
        if (scope.isEmpty()) {
            receiverKind = "this";
        } else {
            Expression receiver = scope.orElseThrow();
            receiverExpr = receiver.toString();
            if (receiver instanceof NameExpr nameExpr) {
                scopeName = nameExpr.getNameAsString();
                receiverKind = "field-or-name";
            } else if (receiver instanceof FieldAccessExpr fieldAccess
                    && fieldAccess.getScope() instanceof ThisExpr) {
                scopeName = fieldAccess.getNameAsString();
                receiverKind = "field-or-name";
            } else if (receiver instanceof ThisExpr) {
                receiverKind = "this";
            } else if (receiver instanceof MethodCallExpr) {
                receiverKind = "chain";
            } else {
                receiverKind = "none";
            }
        }

        json.put("scope", scopeName);
        json.put("receiverKind", receiverKind);
        json.put("receiverExpr", receiverExpr);
        json.put("name", call.getNameAsString());
        json.put("line", nullableStartLine(call));

        List<Map<String, Object>> arguments = new ArrayList<>();
        for (Expression argument : call.getArguments()) {
            Map<String, Object> argumentJson = orderedMap();
            argumentJson.put("expr", argument.toString());
            argumentJson.put("kind", argumentKind(argument));
            arguments.add(argumentJson);
        }
        json.put("arguments", arguments);
        json.put("assignedTo", assignedVariableForCall(call));
        json.put("assignmentChain", assignmentChainForCall(call));

        BranchLocation branchLocation = branchLocation(call, branchByNode);
        json.put("branchId", branchLocation.branchId);
        json.put("branchArm", branchLocation.arm);
        return json;
    }

    private static String argumentKind(Expression argument) {
        if (argument instanceof LiteralExpr || argument instanceof NullLiteralExpr) {
            return "literal";
        }
        if (argument instanceof NameExpr) {
            return "name";
        }
        if (argument instanceof MethodCallExpr) {
            return "methodCall";
        }
        return "other";
    }

    private static List<String> assignmentChainForCall(MethodCallExpr call) {
        List<String> chain = new ArrayList<>();
        Node current = call;
        while (true) {
            Optional<Node> parentOptional = current.getParentNode();
            if (parentOptional.isEmpty()) {
                return chain;
            }
            Node parent = parentOptional.orElseThrow();
            if (parent instanceof MethodCallExpr parentCall
                    && parentCall.getScope().orElse(null) == current) {
                chain.add(parentCall.getNameAsString());
                current = parent;
                continue;
            }
            if (parent instanceof EnclosedExpr enclosed && enclosed.getInner() == current) {
                current = parent;
                continue;
            }
            if (parent instanceof CastExpr cast && cast.getExpression() == current) {
                current = parent;
                continue;
            }
            return chain;
        }
    }

    private static String assignedVariableForCall(MethodCallExpr call) {
        Node current = call;
        while (true) {
            Optional<Node> parentOptional = current.getParentNode();
            if (parentOptional.isEmpty()) {
                return null;
            }
            Node parent = parentOptional.orElseThrow();
            if (parent instanceof MethodCallExpr parentCall
                    && parentCall.getScope().orElse(null) == current) {
                current = parent;
                continue;
            }
            if (parent instanceof EnclosedExpr enclosed && enclosed.getInner() == current) {
                current = parent;
                continue;
            }
            if (parent instanceof CastExpr cast && cast.getExpression() == current) {
                current = parent;
                continue;
            }
            if (parent instanceof VariableDeclarator variable
                    && variable.getInitializer().orElse(null) == current) {
                return variable.getNameAsString();
            }
            return null;
        }
    }

    private static List<BranchDescriptor> extractBranchDescriptors(BlockStmt body) {
        List<Node> nodes = new ArrayList<>();
        nodes.addAll(body.findAll(IfStmt.class));
        nodes.addAll(body.findAll(ConditionalExpr.class));
        nodes.addAll(body.findAll(SwitchStmt.class));
        nodes.addAll(body.findAll(SwitchExpr.class));
        nodes.addAll(body.findAll(CatchClause.class));
        nodes.addAll(body.findAll(ForStmt.class));
        nodes.addAll(body.findAll(ForEachStmt.class));
        nodes.addAll(body.findAll(WhileStmt.class));
        nodes.addAll(body.findAll(DoStmt.class));
        nodes.sort(JavaSymbolExtractor::compareNodes);

        List<BranchDescriptor> descriptors = new ArrayList<>();
        for (int i = 0; i < nodes.size(); i++) {
            descriptors.add(new BranchDescriptor("B" + (i + 1), nodes.get(i)));
        }
        return descriptors;
    }

    private static List<Map<String, Object>> extractMemberReads(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode,
            Set<String> variableFilter
    ) {
        List<Node> candidates = new ArrayList<>();
        candidates.addAll(body.findAll(MethodCallExpr.class));
        candidates.addAll(body.findAll(FieldAccessExpr.class));
        candidates.sort(JavaSymbolExtractor::compareNodes);

        List<Map<String, Object>> reads = new ArrayList<>();
        Set<String> dedupe = new LinkedHashSet<>();
        for (Node candidate : candidates) {
            if (isScopeOfLargerMemberRead(candidate)) {
                continue;
            }
            MemberPath memberPath = memberPath(candidate, true);
            if (memberPath == null || memberPath.variable == null || memberPath.path.isBlank()) {
                continue;
            }
            if (variableFilter != null && !variableFilter.contains(memberPath.variable)) {
                continue;
            }
            String accessKind = candidate instanceof MethodCallExpr ? "getter" : "field";
            Integer line = nullableStartLine(candidate);
            String key = memberPath.variable + "|" + memberPath.path + "|" + accessKind + "|" + line;
            if (!dedupe.add(key)) {
                continue;
            }
            BranchLocation location = branchLocation(candidate, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("variable", memberPath.variable);
            json.put("propertyPath", memberPath.path);
            json.put("accessKind", accessKind);
            json.put("line", line);
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            reads.add(json);
        }
        return reads;
    }

    private static boolean isScopeOfLargerMemberRead(Node node) {
        Optional<Node> parentOptional = node.getParentNode();
        if (parentOptional.isEmpty()) {
            return false;
        }
        Node parent = parentOptional.orElseThrow();
        if (parent instanceof MethodCallExpr methodCall) {
            return methodCall.getScope().orElse(null) == node;
        }
        if (parent instanceof FieldAccessExpr fieldAccess) {
            return fieldAccess.getScope() == node;
        }
        return false;
    }

    private static MemberPath memberPath(Node node, boolean terminal) {
        if (node instanceof NameExpr nameExpr) {
            return new MemberPath(nameExpr.getNameAsString(), "");
        }
        if (node instanceof FieldAccessExpr fieldAccess) {
            MemberPath base = memberPath(fieldAccess.getScope(), false);
            if (base == null) {
                return null;
            }
            String path = appendPath(base.path, fieldAccess.getNameAsString());
            return new MemberPath(base.variable, path);
        }
        if (node instanceof MethodCallExpr methodCall) {
            if (methodCall.getScope().isEmpty()) {
                return null;
            }
            MemberPath base = memberPath(methodCall.getScope().orElseThrow(), false);
            if (base == null) {
                return null;
            }
            String segment = methodCall.getNameAsString() + (terminal ? "" : "()");
            return new MemberPath(base.variable, appendPath(base.path, segment));
        }
        if (node instanceof EnclosedExpr enclosed) {
            return memberPath(enclosed.getInner(), terminal);
        }
        if (node instanceof CastExpr cast) {
            return memberPath(cast.getExpression(), terminal);
        }
        return null;
    }

    private static String appendPath(String current, String segment) {
        return current == null || current.isBlank() ? segment : current + "." + segment;
    }

    private static List<Map<String, Object>> extractLocalVariables(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<VariableDeclarator> variables = new ArrayList<>(body.findAll(VariableDeclarator.class));
        variables.sort(JavaSymbolExtractor::compareNodes);
        List<Map<String, Object>> out = new ArrayList<>();
        for (VariableDeclarator variable : variables) {
            BranchLocation location = branchLocation(variable, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("name", variable.getNameAsString());
            json.put("declaredType", variable.getType().asString());
            json.put("initializer", variable.getInitializer().map(Object::toString).orElse(null));
            json.put("line", nullableStartLine(variable));
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            out.add(json);
        }
        return out;
    }

    private static List<Map<String, Object>> extractAssignments(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<AssignExpr> assignments = new ArrayList<>(body.findAll(AssignExpr.class));
        assignments.sort(JavaSymbolExtractor::compareNodes);
        List<Map<String, Object>> out = new ArrayList<>();
        for (AssignExpr assignment : assignments) {
            BranchLocation location = branchLocation(assignment, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("target", assignment.getTarget().toString());
            json.put("value", assignment.getValue().toString());
            json.put("operator", assignment.getOperator().asString());
            json.put("line", nullableStartLine(assignment));
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            out.add(json);
        }
        return out;
    }

    private static List<Map<String, Object>> extractConfigurationReads(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode,
            Set<String> configurationFieldNames,
            Set<String> parameterNames
    ) {
        if (configurationFieldNames == null || configurationFieldNames.isEmpty()) {
            return List.of();
        }
        Set<String> shadowed = new LinkedHashSet<>(parameterNames == null ? Set.of() : parameterNames);
        for (VariableDeclarator variable : body.findAll(VariableDeclarator.class)) {
            shadowed.add(variable.getNameAsString());
        }

        List<Node> candidates = new ArrayList<>();
        for (NameExpr nameExpr : body.findAll(NameExpr.class)) {
            String name = nameExpr.getNameAsString();
            if (!configurationFieldNames.contains(name) || shadowed.contains(name)) {
                continue;
            }
            if (nameExpr.getParentNode().filter(FieldAccessExpr.class::isInstance).isPresent()) {
                continue;
            }
            candidates.add(nameExpr);
        }
        for (FieldAccessExpr fieldAccess : body.findAll(FieldAccessExpr.class)) {
            if (fieldAccess.getScope() instanceof ThisExpr
                    && configurationFieldNames.contains(fieldAccess.getNameAsString())) {
                candidates.add(fieldAccess);
            }
        }
        candidates.sort(JavaSymbolExtractor::compareNodes);

        List<Map<String, Object>> out = new ArrayList<>();
        Set<String> dedupe = new LinkedHashSet<>();
        for (Node candidate : candidates) {
            String name = candidate instanceof NameExpr nameExpr
                    ? nameExpr.getNameAsString()
                    : ((FieldAccessExpr) candidate).getNameAsString();
            Integer line = nullableStartLine(candidate);
            String key = name + "|" + line + "|" + candidate;
            if (!dedupe.add(key)) {
                continue;
            }
            BranchLocation location = branchLocation(candidate, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("fieldName", name);
            json.put("expression", candidate.toString());
            json.put("qualified", candidate instanceof FieldAccessExpr);
            json.put("line", line);
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            out.add(json);
        }
        return out;
    }

    private static List<Map<String, Object>> extractDereferenceChains(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<Node> candidates = new ArrayList<>();
        candidates.addAll(body.findAll(MethodCallExpr.class));
        candidates.addAll(body.findAll(FieldAccessExpr.class));
        candidates.sort(JavaSymbolExtractor::compareNodes);

        List<Map<String, Object>> out = new ArrayList<>();
        Set<String> dedupe = new LinkedHashSet<>();
        for (Node candidate : candidates) {
            if (isScopeOfLargerMemberRead(candidate)) {
                continue;
            }
            MemberPath path = memberPath(candidate, true);
            if (path == null || path.variable == null || path.path.isBlank()) {
                continue;
            }
            Integer line = nullableStartLine(candidate);
            String key = path.variable + "|" + path.path + "|" + line;
            if (!dedupe.add(key)) {
                continue;
            }
            BranchLocation location = branchLocation(candidate, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("rootVariable", path.variable);
            json.put("propertyPath", path.path);
            json.put("expression", candidate.toString());
            json.put("line", line);
            json.put("endLine", nullableEndLine(candidate));
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            json.put("nonNullPrefixes", nonNullPrefixes(path.variable, path.path));
            putConsumerFacts(json, candidate);
            out.add(json);
        }
        return out;
    }

    private static List<String> nonNullPrefixes(String root, String propertyPath) {
        List<String> prefixes = new ArrayList<>();
        prefixes.add(root);
        String[] segments = propertyPath.split("\\.");
        String current = root;
        for (int index = 0; index < Math.max(0, segments.length - 1); index++) {
            String segment = segments[index];
            current = current + "." + segment;
            prefixes.add(current);
        }
        return prefixes;
    }

    private static void putConsumerFacts(Map<String, Object> json, Node candidate) {
        Node parent = candidate.getParentNode().orElse(null);
        if (parent instanceof MethodCallExpr call && call.getArguments().contains(candidate)) {
            json.put("consumerKind", "methodArgument");
            json.put("consumerName", call.getNameAsString());
            json.put("consumerScope", call.getScope().map(Object::toString).orElse(null));
            json.put("argumentIndex", call.getArguments().indexOf(candidate));
            return;
        }
        if (parent instanceof ReturnStmt) {
            json.put("consumerKind", "return");
        } else if (parent instanceof VariableDeclarator) {
            json.put("consumerKind", "initializer");
        } else if (parent instanceof AssignExpr) {
            json.put("consumerKind", "assignment");
        } else if (parent instanceof BinaryExpr) {
            json.put("consumerKind", "condition-or-expression");
        } else {
            json.put("consumerKind", null);
        }
        json.put("consumerName", null);
        json.put("consumerScope", null);
        json.put("argumentIndex", null);
    }

    private static List<Map<String, Object>> extractLineFacts(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<Statement> statements = body.findAll(Statement.class).stream()
                .filter(statement -> !(statement instanceof BlockStmt))
                .sorted(JavaSymbolExtractor::compareNodes)
                .collect(Collectors.toList());
        List<Map<String, Object>> out = new ArrayList<>();
        for (Statement statement : statements) {
            BranchLocation location = branchLocation(statement, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("kind", statement.getClass().getSimpleName());
            json.put("source", statement.toString());
            json.put("line", nullableStartLine(statement));
            json.put("endLine", nullableEndLine(statement));
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            out.add(json);
        }
        return out;
    }

    private static List<Map<String, Object>> extractNullGuards(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<Map<String, Object>> guards = new ArrayList<>();
        List<Node> guardNodes = new ArrayList<>();
        for (BinaryExpr binary : body.findAll(BinaryExpr.class)) {
            if ((binary.getOperator() == BinaryExpr.Operator.EQUALS
                    || binary.getOperator() == BinaryExpr.Operator.NOT_EQUALS)
                    && (binary.getLeft() instanceof NullLiteralExpr
                    || binary.getRight() instanceof NullLiteralExpr)) {
                guardNodes.add(binary);
            }
        }
        for (MethodCallExpr call : body.findAll(MethodCallExpr.class)) {
            if (call.getScope().map(Object::toString).filter("Objects"::equals).isPresent()
                    && ("isNull".equals(call.getNameAsString()) || "nonNull".equals(call.getNameAsString()))
                    && call.getArguments().size() == 1) {
                guardNodes.add(call);
            }
        }
        guardNodes.sort(JavaSymbolExtractor::compareNodes);

        int counter = 1;
        for (Node node : guardNodes) {
            String expression;
            String operator;
            if (node instanceof BinaryExpr binary) {
                Expression nonNullSide = binary.getLeft() instanceof NullLiteralExpr
                        ? binary.getRight() : binary.getLeft();
                expression = nonNullSide.toString();
                operator = binary.getOperator() == BinaryExpr.Operator.EQUALS ? "==" : "!=";
            } else if (node instanceof MethodCallExpr call) {
                expression = call.getArgument(0).toString();
                operator = "isNull".equals(call.getNameAsString()) ? "==" : "!=";
            } else {
                continue;
            }
            BranchLocation location = branchLocation(node, branchByNode);
            Map<String, Object> json = orderedMap();
            json.put("guardId", "G" + counter++);
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            json.put("expression", expression);
            json.put("operator", operator);
            json.put("comparedWith", "null");
            json.put("line", nullableStartLine(node));
            guards.add(json);
        }
        return guards;
    }

    private static List<Map<String, Object>> extractExits(
            BlockStmt body,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        List<Node> nodes = new ArrayList<>();
        nodes.addAll(body.findAll(ReturnStmt.class));
        nodes.addAll(body.findAll(ThrowStmt.class));
        nodes.sort(JavaSymbolExtractor::compareNodes);

        List<Map<String, Object>> exits = new ArrayList<>();
        for (Node node : nodes) {
            BranchLocation location = branchLocation(node, branchByNode);
            Map<String, Object> json = orderedMap();
            if (node instanceof ReturnStmt returnStmt) {
                json.put("kind", "return");
                json.put("expression", returnStmt.getExpression().map(Object::toString).orElse(null));
            } else if (node instanceof ThrowStmt throwStmt) {
                json.put("kind", "throw");
                json.put("expression", throwStmt.getExpression().toString());
            }
            json.put("line", nullableStartLine(node));
            json.put("branchId", location.branchId);
            json.put("branchArm", location.arm);
            exits.add(json);
        }
        return exits;
    }

    private static BranchLocation branchLocation(
            Node node,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        Node current = node;
        while (current != null) {
            BranchDescriptor descriptor = branchByNode.get(current);
            if (descriptor != null && current != node) {
                return new BranchLocation(descriptor.id, branchArm(descriptor.node, node));
            }
            current = current.getParentNode().orElse(null);
        }
        return new BranchLocation(null, null);
    }

    private static String branchArm(Node branchNode, Node descendant) {
        if (branchNode instanceof IfStmt ifStmt) {
            if (isDescendantOrSelf(ifStmt.getCondition(), descendant)) {
                return "condition";
            }
            if (isDescendantOrSelf(ifStmt.getThenStmt(), descendant)) {
                return "then";
            }
            if (ifStmt.getElseStmt().filter(stmt -> isDescendantOrSelf(stmt, descendant)).isPresent()) {
                return "else";
            }
        } else if (branchNode instanceof ConditionalExpr conditional) {
            if (isDescendantOrSelf(conditional.getCondition(), descendant)) {
                return "condition";
            }
            if (isDescendantOrSelf(conditional.getThenExpr(), descendant)) {
                return "then";
            }
            if (isDescendantOrSelf(conditional.getElseExpr(), descendant)) {
                return "else";
            }
        } else if (branchNode instanceof SwitchStmt switchStmt) {
            return isDescendantOrSelf(switchStmt.getSelector(), descendant) ? "selector" : "case";
        } else if (branchNode instanceof SwitchExpr switchExpr) {
            return isDescendantOrSelf(switchExpr.getSelector(), descendant) ? "selector" : "case";
        } else if (branchNode instanceof CatchClause) {
            return "catch";
        } else if (branchNode instanceof ForStmt forStmt) {
            if (forStmt.getCompare().filter(expr -> isDescendantOrSelf(expr, descendant)).isPresent()) {
                return "condition";
            }
            return isDescendantOrSelf(forStmt.getBody(), descendant) ? "body" : "control";
        } else if (branchNode instanceof ForEachStmt forEachStmt) {
            return isDescendantOrSelf(forEachStmt.getBody(), descendant) ? "body" : "iterable";
        } else if (branchNode instanceof WhileStmt whileStmt) {
            return isDescendantOrSelf(whileStmt.getCondition(), descendant) ? "condition" : "body";
        } else if (branchNode instanceof DoStmt doStmt) {
            return isDescendantOrSelf(doStmt.getCondition(), descendant) ? "condition" : "body";
        }
        return null;
    }

    private static boolean isDescendantOrSelf(Node ancestor, Node node) {
        Node current = node;
        while (current != null) {
            if (current == ancestor) {
                return true;
            }
            current = current.getParentNode().orElse(null);
        }
        return false;
    }

    private static int compareNodes(Node left, Node right) {
        int leftLine = left.getRange().map(range -> range.begin.line).orElse(Integer.MAX_VALUE);
        int rightLine = right.getRange().map(range -> range.begin.line).orElse(Integer.MAX_VALUE);
        int result = Integer.compare(leftLine, rightLine);
        if (result != 0) {
            return result;
        }
        int leftColumn = left.getRange().map(range -> range.begin.column).orElse(Integer.MAX_VALUE);
        int rightColumn = right.getRange().map(range -> range.begin.column).orElse(Integer.MAX_VALUE);
        result = Integer.compare(leftColumn, rightColumn);
        if (result != 0) {
            return result;
        }
        int leftEndLine = left.getRange().map(range -> range.end.line).orElse(Integer.MAX_VALUE);
        int rightEndLine = right.getRange().map(range -> range.end.line).orElse(Integer.MAX_VALUE);
        result = Integer.compare(leftEndLine, rightEndLine);
        if (result != 0) {
            return result;
        }
        int leftEndColumn = left.getRange().map(range -> range.end.column).orElse(Integer.MAX_VALUE);
        int rightEndColumn = right.getRange().map(range -> range.end.column).orElse(Integer.MAX_VALUE);
        result = Integer.compare(leftEndColumn, rightEndColumn);
        if (result != 0) {
            return result;
        }
        return Comparator.comparing((Node n) -> n.getClass().getName())
                .compare(left, right);
    }

    private static final class BranchDescriptor {
        private final String id;
        private final Node node;

        private BranchDescriptor(String id, Node node) {
            this.id = id;
            this.node = node;
        }

        private Map<String, Object> toJson(IdentityHashMap<Node, BranchDescriptor> branchByNode) {
            Map<String, Object> json = orderedMap();
            json.put("branchId", id);
            json.put("kind", branchKind(node));
            json.put("condition", branchCondition(node));
            json.put("line", nullableStartLine(node));
            json.put("endLine", nullableEndLine(node));

            BranchLocation parent = parentBranchLocation(node, branchByNode);
            json.put("parentBranchId", parent.branchId);
            json.put("parentArm", parent.arm);

            if (node instanceof IfStmt ifStmt) {
                putNodeRange(json, "thenStartLine", "thenEndLine", ifStmt.getThenStmt());
                ifStmt.getElseStmt().ifPresentOrElse(
                        stmt -> putNodeRange(json, "elseStartLine", "elseEndLine", stmt),
                        () -> {
                            json.put("elseStartLine", null);
                            json.put("elseEndLine", null);
                        }
                );
            } else if (node instanceof ConditionalExpr conditional) {
                putNodeRange(json, "thenStartLine", "thenEndLine", conditional.getThenExpr());
                putNodeRange(json, "elseStartLine", "elseEndLine", conditional.getElseExpr());
            } else {
                Node bodyNode = branchBody(node);
                if (bodyNode != null) {
                    putNodeRange(json, "bodyStartLine", "bodyEndLine", bodyNode);
                } else {
                    json.put("bodyStartLine", null);
                    json.put("bodyEndLine", null);
                }
            }
            return json;
        }
    }

    private static BranchLocation parentBranchLocation(
            Node branchNode,
            IdentityHashMap<Node, BranchDescriptor> branchByNode
    ) {
        Node current = branchNode.getParentNode().orElse(null);
        while (current != null) {
            BranchDescriptor parent = branchByNode.get(current);
            if (parent != null) {
                return new BranchLocation(parent.id, branchArm(parent.node, branchNode));
            }
            current = current.getParentNode().orElse(null);
        }
        return new BranchLocation(null, null);
    }

    private static String branchKind(Node node) {
        if (node instanceof IfStmt) {
            return "if";
        }
        if (node instanceof ConditionalExpr) {
            return "ternary";
        }
        if (node instanceof SwitchStmt || node instanceof SwitchExpr) {
            return "switch";
        }
        if (node instanceof CatchClause) {
            return "catch";
        }
        return "loop";
    }

    private static String branchCondition(Node node) {
        if (node instanceof IfStmt ifStmt) {
            return ifStmt.getCondition().toString();
        }
        if (node instanceof ConditionalExpr conditional) {
            return conditional.getCondition().toString();
        }
        if (node instanceof SwitchStmt switchStmt) {
            return switchStmt.getSelector().toString();
        }
        if (node instanceof SwitchExpr switchExpr) {
            return switchExpr.getSelector().toString();
        }
        if (node instanceof CatchClause catchClause) {
            return catchClause.getParameter().getType().asString();
        }
        if (node instanceof ForStmt forStmt) {
            return forStmt.getCompare().map(Object::toString).orElse(null);
        }
        if (node instanceof ForEachStmt) {
            return null;
        }
        if (node instanceof WhileStmt whileStmt) {
            return whileStmt.getCondition().toString();
        }
        if (node instanceof DoStmt doStmt) {
            return doStmt.getCondition().toString();
        }
        return null;
    }

    private static Node branchBody(Node node) {
        if (node instanceof CatchClause catchClause) {
            return catchClause.getBody();
        }
        if (node instanceof ForStmt forStmt) {
            return forStmt.getBody();
        }
        if (node instanceof ForEachStmt forEachStmt) {
            return forEachStmt.getBody();
        }
        if (node instanceof WhileStmt whileStmt) {
            return whileStmt.getBody();
        }
        if (node instanceof DoStmt doStmt) {
            return doStmt.getBody();
        }
        return null;
    }

    private static void putNodeRange(
            Map<String, Object> json,
            String startKey,
            String endKey,
            Node node
    ) {
        json.put(startKey, nullableStartLine(node));
        json.put(endKey, nullableEndLine(node));
    }

    private static final class MemberPath {
        private final String variable;
        private final String path;

        private MemberPath(String variable, String path) {
            this.variable = variable;
            this.path = path;
        }
    }

    private static final class BranchLocation {
        private final String branchId;
        private final String arm;

        private BranchLocation(String branchId, String arm) {
            this.branchId = branchId;
            this.arm = arm;
        }
    }

    private static Set<String> modifiers(NodeList<Modifier> modifiers) {
        Set<String> out = new LinkedHashSet<>();
        for (Modifier modifier : modifiers) {
            out.add(modifier.getKeyword().asString());
        }
        return out;
    }

    private static List<String> annotationNames(NodeList<AnnotationExpr> annotations) {
        return annotations.stream()
                .map(annotation -> annotation.getName().getIdentifier())
                .collect(Collectors.toList());
    }

    private static List<String> annotationExprs(NodeList<AnnotationExpr> annotations) {
        return annotations.stream()
                .map(AnnotationExpr::toString)
                .collect(Collectors.toList());
    }

    private static Optional<Integer> startLine(Node node) {
        return node.getRange().map(range -> range.begin.line);
    }

    private static Optional<Integer> endLine(Node node) {
        return node.getRange().map(range -> range.end.line);
    }

    private static Integer nullableStartLine(Node node) {
        return startLine(node).orElse(null);
    }

    private static Integer nullableEndLine(Node node) {
        return endLine(node).orElse(null);
    }

    private static <T> T firstOrNull(List<T> values) {
        return values == null || values.isEmpty() ? null : values.get(0);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> orderedMap() {
        return new LinkedHashMap<>();
    }

    private static String toJson(Object value) {
        StringBuilder sb = new StringBuilder(4096);
        appendJson(sb, value);
        return sb.toString();
    }

    private static void appendJson(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String string) {
            appendJsonString(sb, string);
        } else if (value instanceof Number || value instanceof Boolean) {
            sb.append(value);
        } else if (value instanceof Map<?, ?> map) {
            appendJsonObject(sb, map);
        } else if (value instanceof Iterable<?> iterable) {
            appendJsonArray(sb, iterable);
        } else {
            appendJsonString(sb, String.valueOf(value));
        }
    }

    private static void appendJsonObject(StringBuilder sb, Map<?, ?> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            appendJsonString(sb, String.valueOf(entry.getKey()));
            sb.append(':');
            appendJson(sb, entry.getValue());
            first = false;
        }
        sb.append('}');
    }

    private static void appendJsonArray(StringBuilder sb, Iterable<?> values) {
        sb.append('[');
        boolean first = true;
        for (Object value : values) {
            if (!first) {
                sb.append(',');
            }
            appendJson(sb, value);
            first = false;
        }
        sb.append(']');
    }

    private static void appendJsonString(StringBuilder sb, String value) {
        sb.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    private static final class ParseFailureException extends RuntimeException {
        private ParseFailureException(String message) {
            super(message);
        }
    }
}
