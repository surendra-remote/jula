"""Per-style few-shot exemplars so the model copies the right shape.

Provides modern Spring Boot 3.5+, Java 17, and JUnit 5 production-grade blueprint variations
featuring advanced edge cases and exception handling to drive branch coverage past 80%.
"""

from __future__ import annotations

# Standard Java 17 / Mockito 5 Unit Testing Blueprint
_MOCKITO_UNIT = """\
package com.example.service;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.when;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.times;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;

@ExtendWith(MockitoExtension.class)
class RateServiceTest {

    @Mock
    private RateClient rateClient;

    @InjectMocks
    private RateService rateService;

    @Test
    @DisplayName("Should successfully convert currency when valid parameters are provided")
    void convertsUsingFetchedRate() {
        // Happy Path
        when(rateClient.rate("USD", "INR")).thenReturn(83.0);
        
        double result = rateService.toInr(100.0, "USD");
        
        assertEquals(8300.0, result);
        verify(rateClient, times(1)).rate("USD", "INR");
    }

    @Test
    @DisplayName("Should throw IllegalArgumentException when processing a negative amount boundary")
    void rejectsNegativeAmount() {
        assertThrows(IllegalArgumentException.class, () -> rateService.toInr(-1.0, "USD"));
    }
}
"""

# Plain Java 17 Utilities Blueprint (High branch coverage demo)
_PLAIN_UNIT = """\
package com.example.util;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.DisplayName;

class StringsTest {

    @Test
    @DisplayName("Branch Test - Empty inputs should resolve cleanly to Null bounds")
    void stripsBlankToNull_HappyAndEdgePaths() {
        assertNull(Strings.stripToNull("   "));
        assertNull(Strings.stripToNull(""));
        assertEquals("a", Strings.stripToNull("  a "));
    }

    @Test
    @DisplayName("Branch Test - Verify strict null checks inside utilities")
    void isBlank_HandlesNullValues() {
        assertTrue(Strings.isBlank(null));
    }

    @Test
    @DisplayName("Exception Test - Rejects execution loops with validation checks")
    void rejectsNegativeRepeat_ValidationPaths() {
        assertThrows(IllegalArgumentException.class, () -> Strings.repeat("a", -1));
    }
}
"""

_REACTIVE_UNIT = """\
package com.example.reactive;

import static org.mockito.Mockito.when;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import reactor.core.publisher.Mono;
import reactor.test.StepVerifier;

@ExtendWith(MockitoExtension.class)
class TokenServiceTest {

    @Mock
    private TokenClient tokenClient;

    @InjectMocks
    private TokenService tokenService;

    @Test
    void emitsToken() {
        when(tokenClient.fetch("u")).thenReturn(Mono.just("t"));
        StepVerifier.create(tokenService.tokenFor("u"))
                .expectNext("t")
                .verifyComplete();
    }
}
"""

# Spring Boot 3.5 Controller Blueprint (standalone MockMvc, no Spring context)
_STANDALONE_MOCKMVC = """\
package com.example.web;

import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.InjectMocks;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

@ExtendWith(MockitoExtension.class)
class RateControllerTest {

    @Mock
    private RateService rateService;

    @InjectMocks
    private RateController rateController;

    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        mockMvc = MockMvcBuilders.standaloneSetup(rateController).build();
    }

    @Test
    void returnsConvertedAmount() throws Exception {
        RateResponse response = new RateResponse();
        response.setAmount(83.0);
        response.setCurrency("INR");
        when(rateService.toInr(eq(1.0), eq("USD"))).thenReturn(response);

        mockMvc.perform(get("/rates").param("amount", "1.0").param("from", "USD"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.amount").value(83.0))
                .andExpect(jsonPath("$.currency").value("INR"));
    }
}
"""


EXEMPLARS = {
    # Keys MUST match stack/classifier.py style strings exactly. A missing key
    # silently falls back to _PLAIN_UNIT (which has no mocks at all), so any new
    # classifier style must be added here.
    "service-mockito": _MOCKITO_UNIT,
    "validator-mockito": _MOCKITO_UNIT,
    "controller-standalone-mockmvc": _STANDALONE_MOCKMVC,
    "plain-unit": _PLAIN_UNIT,
    "entity-pojo": _PLAIN_UNIT,
    "dto-pojo": _PLAIN_UNIT,
    "exception-unit": _PLAIN_UNIT,
    "mapper-unit": _PLAIN_UNIT,
    "validator-unit": _PLAIN_UNIT,
    "utility-unit": _PLAIN_UNIT,
    "reactive-unit": _REACTIVE_UNIT,
}


def exemplar_for(style: str) -> str:
    # Default to pure JUnit.  Never default to Mockito/Spring templates for
    # unknown styles because that reintroduces fragile imports/annotations.
    return EXEMPLARS.get(style, _PLAIN_UNIT)